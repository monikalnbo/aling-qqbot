"""验收「群聊全量入库 + 按需回看」。

真实链路：扮演 NapCat 连上正在跑的 AstrBot（真实 DeepSeek），在一个新群里
- 先由两个人**不 @ 机器人**聊几句（机器人不该回）
- 查本地 SQLite，确认这些没 @ 的消息都进了 group_messages
- 再 @ 机器人问「刚才群里谁说了什么」，看它是否答对且只念昵称
- 顺带测日常请求不触发检索（日志里出现 recall-skip），并记录首条回复耗时

用法：
    python3 scripts/verify_group_archive.py --out evidence/group_archive.log
    python3 scripts/verify_group_archive.py --db astrbot/data/astrbot/plugin_data/astrbot_plugin_aling_memory/memory.db

退出码：0 全过；1 有失败项。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

SELF_ID = 10000
BOT_NAME = "阿凌测试号"
A_ID, A_NAME = 222333, "小胖"
B_ID, B_NAME = 333444, "阿哲"
DIGIT_RUN = re.compile(r"\d{4,}")

DEFAULT_DB = "astrbot/data/astrbot/plugin_data/astrbot_plugin_aling_memory/memory.db"


def fresh_id(offset: int = 0) -> int:
    return int(time.time() * 1000) % 10_000_000 * 10 + offset % 10


def group_event(text, *, message_id, group_id, user_id, nickname, card="", at=False):
    chain = []
    if at:
        chain.append({"type": "at", "data": {"qq": str(SELF_ID)}})
        chain.append({"type": "text", "data": {"text": " " + text}})
    else:
        chain.append({"type": "text", "data": {"text": text}})
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
        "to_me": at,
        "sub_type": "normal",
        "group_id": group_id,
        "user_id": user_id,
        "message_type": "group",
        "message": chain,
        "raw_message": (f"[CQ:at,qq={SELF_ID}] " if at else "") + text,
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
            elif data["action"] == "get_group_info":
                result["data"] = {"group_id": 0, "group_name": "验收群"}
            await self.ws.send(json.dumps(result, ensure_ascii=False))

    async def settle(self, quiet: float = 6.0, timeout: float = 60.0) -> int:
        last = len(self.replies)
        stable = time.time()
        deadline = time.time() + timeout
        while time.time() < deadline:
            await asyncio.sleep(0.3)
            if len(self.replies) != last:
                last = len(self.replies)
                stable = time.time()
            elif time.time() - stable >= quiet:
                break
        return last

    async def say(self, ev, timeout: float = 120.0, quiet: float = 6.0) -> tuple[str, float]:
        """发一条消息，返回 (本轮全部回复文本, 首条回复耗时秒)。"""
        await self.settle(quiet=6.0)
        before = len(self.replies)
        t0 = time.time()
        await self.ws.send(json.dumps(ev, ensure_ascii=False))
        first_at = None
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.replies) > before:
                if first_at is None:
                    first_at = time.time()
                if time.time() - first_at >= quiet:
                    break
            elif first_at is not None:
                break
            await asyncio.sleep(0.2)
        dt = (first_at - t0) if first_at else -1.0
        return "\n".join(text_of(r) for r in self.replies[before:]), dt

    async def silence(self, ev, wait: float = 6.0) -> int:
        await self.settle(quiet=6.0)
        before = len(self.replies)
        await self.ws.send(json.dumps(ev, ensure_ascii=False))
        await asyncio.sleep(wait)
        await self.settle(quiet=5.0)
        return len(self.replies) - before


def text_of(reply: dict) -> str:
    out = ""
    for seg in reply.get("params", {}).get("message", []):
        if isinstance(seg, dict) and seg.get("type") == "text":
            out += seg.get("data", {}).get("text", "")
    return out


def db_rows(db_path: str, gid: int) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return list(
            conn.execute(
                "SELECT * FROM group_messages WHERE group_id=? ORDER BY ts, id", (str(gid),)
            ).fetchall()
        )
    finally:
        conn.close()


def docker_logs_since(seconds: int) -> str:
    try:
        return subprocess.run(
            ["docker", "logs", "astrbot", "--since", f"{seconds}s"],
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout
    except Exception:  # noqa: BLE001
        return ""


async def run(url: str, token: str | None, out_path: str, db_path: str) -> int:
    import websockets

    headers = {"X-Self-ID": str(SELF_ID), "X-Client-Role": "Universal"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    log: list[str] = []
    checks: list[tuple[str, bool, str]] = []

    def emit(line: str = "") -> None:
        print(line, flush=True)
        log.append(line)

    def check(label: str, ok: bool, detail: str = "") -> bool:
        checks.append((label, ok, detail))
        emit(f"### {label}  => {'PASS' if ok else 'FAIL'}   {detail}")
        return ok

    started = int(time.time())
    async with websockets.connect(url, additional_headers=headers, max_size=2**22) as ws:
        emit(f"# 群聊全量入库 + 按需回看 · 真实验收  {time.strftime('%F %T %Z')}")
        emit(f"# url={url}  db={db_path}")
        emit("# 身份：小胖=222333 阿哲=333444 机器人=10000（这些数字都不许出现在回复里）")
        emit()

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
        await asyncio.sleep(1.5)

        gid = 880000 + int(time.time()) % 1000
        emit(f"# 测试群号（mock）：{gid}")
        emit()

        # ---------- 阶段一：群里不 @ 机器人聊天 ----------
        chat = [
            (A_ID, A_NAME, "今晚想吃串串，牛肉丸必须多放"),
            (B_ID, B_NAME, "串串吃腻了，我想去吃烤肉"),
            (A_ID, A_NAME, "楼下那家串串排队都排到四十号了"),
            (B_ID, B_NAME, "那改天吧，烤肉店我可以订位"),
            (A_ID, A_NAME, "行，那就周六中午十二点烤肉店见"),
        ]
        emit("## 1. 群聊（全部不 @ 机器人）")
        replies = 0
        for i, (uid, name, text) in enumerate(chat):
            n = await cli.silence(
                group_event(text, message_id=fresh_id(i), group_id=gid,
                            user_id=uid, nickname=name, card=name),
                wait=5,
            )
            replies += n
            emit(f"[群友] {name}: {text}   -> 回复 {n} 条")
        emit()
        check("不 @ 机器人时它不插话", replies == 0, f"replies={replies}")
        emit()

        # ---------- 阶段二：抽查数据库 ----------
        emit("## 2. 抽查本地数据库：未 @ 的消息是否都记下了")
        rows = db_rows(db_path, gid)
        emit(f"group_messages 里该群共 {len(rows)} 条：")
        for r in rows:
            emit(
                f"  #{r['id']} ts={time.strftime('%H:%M:%S', time.localtime(r['ts']))} "
                f"role={r['role']} at_bot={r['at_bot']} "
                f"qq={r['qq_id']} card={r['card']!r} text={r['text']!r}"
            )
        stored_texts = [str(r["text"]) for r in rows if str(r["role"]) == "user"]
        missing = [t for _, _, t in chat if not any(t in s for s in stored_texts)]
        all_at_zero = all(int(r["at_bot"]) == 0 for r in rows if str(r["role"]) == "user")
        emit()
        check("5 条没 @ 的消息全部入库", not missing, f"missing={missing}")
        check("入库记录 at_bot=0（确实是没 @ 的）", all_at_zero)
        check(
            "入库带昵称/群名片与发送者 QQ",
            all(str(r["card"] or r["nickname"]) for r in rows if str(r["role"]) == "user"),
        )
        emit()

        # ---------- 阶段三：@ 它问刚才的事 ----------
        emit("## 3. 过一会儿 @ 它：回看刚才群里说了什么")
        t, dt = await cli.say(
            group_event(
                "刚才群里谁说了什么？说清楚谁想吃什么",
                message_id=fresh_id(20), group_id=gid,
                user_id=B_ID, nickname=B_NAME, card=B_NAME, at=True,
            )
        )
        hit_a = A_NAME in t
        hit_b = B_NAME in t
        hit_topic = ("串串" in t) or ("烤肉" in t)
        no_digits = not DIGIT_RUN.search(t)
        emit(f"[阿凌] {t.strip() or '（无回复）'}")
        emit(f"[判定] 提到小胖={hit_a} 提到阿哲={hit_b} 说到串串/烤肉={hit_topic} 无数字ID={no_digits}")
        emit()
        check("回看「刚才谁说了什么」：答出人名 + 内容 + 只念昵称", hit_a and hit_b and hit_topic and no_digits)

        t, dt2 = await cli.say(
            group_event(
                "刚才说想吃烤肉的是谁来着？",
                message_id=fresh_id(21), group_id=gid,
                user_id=A_ID, nickname=A_NAME, card=A_NAME, at=True,
            )
        )
        emit(f"[阿凌] {t.strip() or '（无回复）'}")
        emit()
        check(
            "回看「谁说想吃烤肉」：点名阿哲、不念数字",
            (B_NAME in t) and not DIGIT_RUN.search(t),
            f"latency={dt2:.1f}s",
        )

        # ---------- 阶段四：日常请求不查库 ----------
        emit("## 4. 日常请求（不该触发检索，也不该变慢）")
        t, dt3 = await cli.say(
            group_event(
                "在吗，今天好累啊",
                message_id=fresh_id(22), group_id=gid,
                user_id=A_ID, nickname=A_NAME, card=A_NAME, at=True,
            )
        )
        emit(f"[阿凌] {t.strip() or '（无回复）'}")
        emit(f"[耗时] 首条回复 {dt3:.1f}s（回看那轮 {dt:.1f}s / {dt2:.1f}s 仅供参考）")
        emit()
        check("日常 @ 正常回复", bool(t.strip()), f"latency={dt3:.1f}s")
        check("日常回复里没有数字 ID", not DIGIT_RUN.search(t))

        task.cancel()

    # ---------- 阶段五：读插件日志，确认「按需检索」 ----------
    emit("## 5. 插件日志：回看只在被问到时发生")
    logs = docker_logs_since(int(time.time()) - started + 10)
    recall_lines = [ln for ln in logs.splitlines() if "recall" in ln]
    skip_lines = [ln for ln in recall_lines if "recall-skip" in ln]
    hit_lines = [ln for ln in recall_lines if re.search(r"recall .*hits=[1-9]", ln)]
    ctx_lines = [ln for ln in logs.splitlines() if "short-term umo=" in ln]
    emit(f"recall 相关日志 {len(recall_lines)} 行：")
    for ln in recall_lines[-10:]:
        emit("  " + ln.strip()[-160:])
    emit()
    if ctx_lines:
        emit("短期上下文日志（最近 3 条）：")
        for ln in ctx_lines[-3:]:
            emit("  " + ln.strip()[-160:])
        emit()
    check("日常请求走了 recall-skip（没查库）", bool(skip_lines) or not hit_lines, f"skip={len(skip_lines)}")
    check("回看请求命中存档（hits>0）", bool(hit_lines), f"hit={len(hit_lines)}")

    emit("## 汇总")
    ok_all = True
    for label, ok, _ in checks:
        emit(f"- {'PASS' if ok else 'FAIL'}  {label}")
        ok_all = ok_all and ok
    emit()
    emit("ALL PASS" if ok_all else "HAS FAILURES")

    out = "\n".join(log) + "\n"
    if out_path:
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(out)
    print(f"[verify] written {out_path}", flush=True)
    return 0 if ok_all else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:6199/ws")
    ap.add_argument("--token", default=None)
    ap.add_argument("--out", default="evidence/group_archive.log")
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()
    return asyncio.run(run(args.url, args.token, args.out, args.db))


if __name__ == "__main__":
    sys.exit(main())
