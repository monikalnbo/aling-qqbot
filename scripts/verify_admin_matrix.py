"""验证管理员指令在真实链路下的实际权限边界（v4.29.4）。

覆盖：
- /reset、/new 在本部署（unique_session=false）下是否只有管理员能用
- /help、/sid、/stop、/stats、/set、/unset 普通成员能否使用
- /mem 插件指令里跨用户读写（facts/forget 指定别人的 QQ）是否越权

用法：
    python3 scripts/verify_admin_matrix.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import importlib.util  # noqa: E402
import pathlib  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "va", pathlib.Path(__file__).with_name("verify_admin.py")
)
va = importlib.util.module_from_spec(spec)
spec.loader.exec_module(va)

SELF_ID = va.SELF_ID
ADMIN_ID = va.ADMIN_ID
MEMBER_ID = va.MEMBER_ID


def ev_group(text: str, mid: int, gid: int, uid: int, nick: str) -> dict:
    return va.group_at_event(
        text, message_id=mid, group_id=gid, user_id=uid, nickname=nick
    )


def ev_private(text: str, mid: int, uid: int, nick: str) -> dict:
    return va.private_event(text, message_id=mid, user_id=uid, nickname=nick)


async def run(url: str) -> int:
    import websockets

    headers = {"X-Self-ID": str(SELF_ID), "X-Client-Role": "Universal"}
    rows: list[tuple[str, str, bool, str]] = []
    async with websockets.connect(url, additional_headers=headers, max_size=2**22) as ws:
        cli = va.Client(ws)
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
        gid = 880100 + int(time.time()) % 400
        n = 0

        async def probe(tag: str, ev: dict, expect_denied: bool, deny_marker="权限不足", timeout=90.0):
            nonlocal n
            n += 1
            r = await cli.ask(ev, timeout=timeout)
            r = r or "（无回复）"
            denied = deny_marker in r
            ok = denied == expect_denied
            print(f"[{tag}] {r[:200]}", flush=True)
            rows.append((tag, r, ok, "被拒" if denied else "放行"))
            return r

        # 开一个会话上下文，方便 /reset、/new 有效果
        await probe(
            "管理员先建上下文 /name 会话别名",
            ev_private("/name 会话别名", 1, ADMIN_ID, "管理员"),
            False,
        )
        # /reset、/new：SHARED_GROUP_ADMIN，本部署 unique_session=false
        await probe("普通成员 群 /reset", ev_group("/reset", 2, gid, MEMBER_ID, "路人甲"), True)
        await probe("管理员 群 /reset", ev_group("/reset", 3, gid, ADMIN_ID, "管理员"), False)
        await probe("普通成员 群 /new", ev_group("/new", 4, gid, MEMBER_ID, "路人甲"), True)
        await probe("管理员 群 /new", ev_group("/new", 5, gid, ADMIN_ID, "管理员"), False)
        # 普通成员可用的指令
        await probe("普通成员 群 /help", ev_group("/help", 6, gid, MEMBER_ID, "路人甲"), False)
        await probe("普通成员 群 /sid", ev_group("/sid", 7, gid, MEMBER_ID, "路人甲"), False)
        await probe("普通成员 群 /stats", ev_group("/stats", 8, gid, MEMBER_ID, "路人甲"), False)
        await probe("普通成员 群 /stop", ev_group("/stop", 9, gid, MEMBER_ID, "路人甲"), False)
        await probe("普通成员 群 /set a b", ev_group("/set a b", 10, gid, MEMBER_ID, "路人甲"), False)
        await probe("普通成员 群 /unset a", ev_group("/unset a", 11, gid, MEMBER_ID, "路人甲"), False)
        # /mem 越权检查：普通成员读/删别人的长期条目
        await probe(
            f"普通成员 私聊 /mem facts {ADMIN_ID}（读别人）",
            ev_private(f"/mem facts {ADMIN_ID}", 12, MEMBER_ID, "路人甲"),
            True,
            "只能查/删你自己",
        )
        await probe(
            "普通成员 私聊 /mem forget 999999001（删别人）",
            ev_private("/mem forget 999999001", 13, MEMBER_ID, "路人甲"),
            True,
            "只能查/删你自己",
        )
        await probe(
            "普通成员 私聊 /mem forget（不带参数=清空自己）",
            ev_private("/mem forget", 14, MEMBER_ID, "路人甲"),
            False,
        )
        await probe(
            "普通成员 私聊 /mem status",
            ev_private("/mem status", 15, MEMBER_ID, "路人甲"),
            True,
            "记忆总览只有管理员能看",
        )
        await probe(
            "管理员 私聊 /mem status",
            ev_private("/mem status", 16, ADMIN_ID, "管理员"),
            False,
        )
        await probe(
            f"管理员 私聊 /mem facts {MEMBER_ID}",
            ev_private(f"/mem facts {MEMBER_ID}", 17, ADMIN_ID, "管理员"),
            False,
        )
        task.cancel()

    print("\n=== 权限矩阵 ===", flush=True)
    for tag, r, ok, verdict in rows:
        print(f"{verdict}\t{tag}\t{r.splitlines()[0][:120] if r else ''}", flush=True)
    print(f"group={gid}", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:6199/ws")
    a = ap.parse_args()
    return asyncio.run(run(a.url))


if __name__ == "__main__":
    sys.exit(main())
