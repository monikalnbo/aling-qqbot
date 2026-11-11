"""验证 AstrBot v4.29.4 的管理员机制：admins_id 里的 QQ 能执行管理员指令，普通号被拒。

用法：
    python3 scripts/verify_admin.py \
        --out evidence/admin_verify.log

检查项：
1. 管理员 <你的管理员QQ号> 私聊 /provider        -> 应输出 Provider 列表
2. 普通号 333444 私聊 /provider            -> 应被拒（权限不足）
3. 管理员 <你的管理员QQ号> 群聊 @ /sid           -> /sid 对所有人生效（不是管理员指令，仅作对照）
4. 管理员 <你的管理员QQ号> 群聊 @ /provider      -> 群聊里也应放行
5. 普通号 333444 群聊 @ /provider          -> 群聊里也应被拒
6. 普通号 333444 私聊 /name 测试别名       -> 被拒（ADMIN 级）
7. 管理员 <你的管理员QQ号> 私聊 /name 管理员别名 -> 生效（ADMIN 级，另一位指令）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

SELF_ID = 10000
BOT_NAME = "大肥鱼测试号"
ADMIN_ID = <你的管理员QQ号>
MEMBER_ID = 333444


def fresh_id(offset: int = 0) -> int:
    return int(time.time() * 1000) % 10_000_000 * 10 + offset % 10


def private_event(text: str, *, message_id: int, user_id: int, nickname: str) -> dict:
    return {
        "time": int(time.time()),
        "self_id": SELF_ID,
        "post_type": "message",
        "message_id": message_id,
        "font": 0,
        "sender": {"user_id": user_id, "nickname": nickname},
        "to_me": True,
        "sub_type": "friend",
        "user_id": user_id,
        "message_type": "private",
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text,
    }


def group_at_event(
    text: str, *, message_id: int, group_id: int, user_id: int, nickname: str
) -> dict:
    return {
        "time": int(time.time()),
        "self_id": SELF_ID,
        "post_type": "message",
        "message_id": message_id,
        "font": 0,
        "sender": {"user_id": user_id, "nickname": nickname, "card": nickname},
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
            elif data["action"] == "get_stranger_info":
                result["data"] = {"user_id": data.get("params", {}).get("user_id")}
            await self.ws.send(json.dumps(result, ensure_ascii=False))

    async def ask(self, ev: dict, timeout: float = 120.0) -> str | None:
        before = len(self.replies)
        await self.ws.send(json.dumps(ev, ensure_ascii=False))
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.replies) > before:
                # /provider 会先发一条"正在探测"，等静默 1.5s 再收尾
                await asyncio.sleep(1.5)
                texts = [text_of(r) for r in self.replies[before:]]
                texts = [t for t in texts if t]
                return "\n".join(texts)
            await asyncio.sleep(0.2)
        return None


def text_of(reply: dict) -> str:
    out = ""
    for seg in reply.get("params", {}).get("message", []):
        if isinstance(seg, dict) and seg.get("type") == "text":
            out += seg.get("data", {}).get("text", "")
    return out.strip()


async def run(url: str) -> int:
    import websockets

    headers = {"X-Self-ID": str(SELF_ID), "X-Client-Role": "Universal"}
    checks: list[tuple[str, bool, str]] = []
    transcript: list[str] = []

    async with websockets.connect(url, additional_headers=headers, max_size=2**22) as ws:
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
        gid = 770100 + int(time.time()) % 400

        def note(tag: str, text: str | None):
            line = f"[{tag}] {text}"
            print(line, flush=True)
            transcript.append(line)

        # 1 管理员私聊 /provider（ADMIN）
        r = await cli.ask(
            private_event("/provider", message_id=fresh_id(1), user_id=ADMIN_ID, nickname="管理员")
        )
        note("admin-private-provider", r or "（无回复）")
        ok1 = bool(r) and ("LLM Providers" in r or "Provider" in r)
        checks.append(("1 管理员私聊 /provider 放行", ok1, r or ""))

        # 2 普通号私聊 /provider（ADMIN）
        r = await cli.ask(
            private_event("/provider", message_id=fresh_id(2), user_id=MEMBER_ID, nickname="路人甲")
        )
        note("member-private-provider", r or "（无回复）")
        ok2 = bool(r) and "权限不足" in r
        checks.append(("2 普通号私聊 /provider 被拒", ok2, r or ""))

        # 3 /sid 对照：不是管理员指令，谁都可用
        r = await cli.ask(
            group_at_event(
                "/sid",
                message_id=fresh_id(3),
                group_id=gid,
                user_id=ADMIN_ID,
                nickname="管理员",
            )
        )
        note("admin-group-sid", r or "（无回复）")
        ok3 = bool(r) and "UID" in r and str(ADMIN_ID) in r
        checks.append(("3 群聊 /sid 对照（非管理员指令）", ok3, r or ""))

        # 4 管理员群聊 /provider
        r = await cli.ask(
            group_at_event(
                "/provider",
                message_id=fresh_id(4),
                group_id=gid,
                user_id=ADMIN_ID,
                nickname="管理员",
            )
        )
        note("admin-group-provider", r or "（无回复）")
        ok4 = bool(r) and ("LLM Providers" in r or "Provider" in r)
        checks.append(("4 管理员群聊 /provider 放行", ok4, r or ""))

        # 5 普通号群聊 /provider
        r = await cli.ask(
            group_at_event(
                "/provider",
                message_id=fresh_id(5),
                group_id=gid,
                user_id=MEMBER_ID,
                nickname="路人甲",
            )
        )
        note("member-group-provider", r or "（无回复）")
        ok5 = bool(r) and "权限不足" in r
        checks.append(("5 普通号群聊 /provider 被拒", ok5, r or ""))

        # 6 普通号 /name（ADMIN）
        r = await cli.ask(
            private_event("/name 路人别名", message_id=fresh_id(6), user_id=MEMBER_ID, nickname="路人甲")
        )
        note("member-private-name", r or "（无回复）")
        ok6 = bool(r) and "权限不足" in r
        checks.append(("6 普通号 /name 被拒", ok6, r or ""))

        # 7 管理员 /name（ADMIN）
        r = await cli.ask(
            private_event("/name 管理员别名", message_id=fresh_id(7), user_id=ADMIN_ID, nickname="管理员")
        )
        note("admin-private-name", r or "（无回复）")
        ok7 = bool(r) and ("UMO name set" in r or "管理员别名" in r)
        checks.append(("7 管理员 /name 放行", ok7, r or ""))

        task.cancel()

    print("\n=== 汇总 ===", flush=True)
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}\n      -> {detail[:300]}", flush=True)
    all_ok = all(ok for _, ok, _ in checks)
    print(f"group={gid}", flush=True)
    print(f"exit={0 if all_ok else 1}", flush=True)
    return 0 if all_ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:6199/ws")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    code = asyncio.run(run(args.url))
    return code


if __name__ == "__main__":
    sys.exit(main())
