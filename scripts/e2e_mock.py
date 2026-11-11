"""端到端自测：不接 QQ，也不花钱调 DeepSeek。

启动真实 NoneBot（反向 WS :8080），再扮演 NapCat 连上来推送一条私聊消息，
验证整条链路：协议端 → NoneBot → 插件 → LLM 客户端 → 回发消息。

用法：
    python scripts/e2e_mock.py            # 内部 mock DeepSeek，验证链路闭环
    python scripts/e2e_mock.py --real     # 使用 .env 里真实 DeepSeek Key 调一次
    python scripts/e2e_mock.py --no-llm   # 只验证协议链路，不调用模型
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MODE = "mock"
if "--real" in sys.argv:
    MODE = "real"
elif "--no-llm" in sys.argv:
    MODE = "none"

os.environ["DAFEIYU_DEEPSEEK_API_KEY"] = "" if MODE == "real" else "sk-e2e-mock"
os.environ["DRIVER"] = "~fastapi+~websockets+~httpx"  # 强制，避免继承外部环境
os.environ["PORT"] = "8080"
os.environ["HOST"] = "127.0.0.1"
os.environ.setdefault("LOG_LEVEL", "INFO")
os.environ.setdefault("DAFEIYU_SHOW_TYPING", "false")

SELF_ID = 10000
RESULT: dict = {}


def run_bot():
    import nonebot
    from nonebot.adapters.onebot.v11 import Adapter

    # 配置文件在项目根目录，bot.py 也从这里启动
    os.chdir(ROOT)
    nonebot.init()
    nonebot.get_driver().register_adapter(Adapter)
    nonebot.load_plugin("dafeiyu")

    if MODE == "mock":
        from dafeiyu.llm import DeepSeekClient

        plugin = sys.modules["dafeiyu"]

        class StubLLM:
            calls = 0
            last_messages: list = []

            async def chat(self, messages):
                StubLLM.calls += 1
                StubLLM.last_messages = messages
                user_text = messages[-1]["content"]
                history_len = len(messages) - 2  # 去掉 system 与本条 user
                return (
                    f"[mock大脑] 收到「{user_text}」，鱼在呢～"
                    f"（第{StubLLM.calls}次，带历史{history_len}条）"
                )

            async def close(self):
                return None

        stub = StubLLM()
        plugin.APP.engine.client = stub
        plugin.APP.client = stub
    elif MODE == "none":
        raise SystemExit("--no-llm 模式请配合 --real 使用；当前仅支持 mock / real")

    nonebot.run()


async def fake_napcat():
    import websockets

    await asyncio.sleep(2.0)  # 等 NoneBot 起来
    uri = "ws://127.0.0.1:8080/onebot/v11/ws"
    headers = {"X-Self-ID": str(SELF_ID), "X-Client-Role": "Universal"}
    async with websockets.connect(uri, additional_headers=headers) as ws:
        # NoneBot 会先握手或直接等事件
        private_event = {
            "time": int(time.time()),
            "self_id": SELF_ID,
            "post_type": "message",
            "sub_type": "friend",
            "user_id": 123456,
            "message_type": "private",
            "message_id": 1,
            "message": [{"type": "text", "data": {"text": "你好，我叫小明"}}],
            "original_message": [],
            "raw_message": "你好，我叫小明",
            "font": 0,
            "sender": {"user_id": 123456, "nickname": "小明"},
            "to_me": True,
        }
        group_event = {
            "time": int(time.time()),
            "self_id": SELF_ID,
            "post_type": "message",
            "sub_type": "normal",
            "group_id": 555111,
            "user_id": 222333,
            "message_type": "group",
            "message_id": 2,
            "message": [{"type": "text", "data": {"text": "今天天气不错"}}],
            "original_message": [],
            "raw_message": "今天天气不错",
            "font": 0,
            "sender": {"user_id": 222333, "nickname": "小红", "card": ""},
            "to_me": False,
        }

        group_at_event = dict(group_event)
        group_at_event.update(
            {
                "message_id": 3,
                "message": [
                    {"type": "at", "data": {"qq": str(SELF_ID)}},
                    {"type": "text", "data": {"text": " 在吗大肥鱼"}},
                ],
                "raw_message": f"[CQ:at,qq={SELF_ID}] 在吗大肥鱼",
                "to_me": True,
            }
        )
        keyword_event = dict(group_event)
        keyword_event.update(
            {
                "message_id": 4,
                "user_id": 444555,
                "message": [{"type": "text", "data": {"text": "大肥鱼今天吃啥"}}],
                "raw_message": "大肥鱼今天吃啥",
                "sender": {"user_id": 444555, "nickname": "小绿", "card": ""},
                "to_me": False,
            }
        )

        second_private = dict(private_event)
        second_private.update(
            {
                "message_id": 5,
                "message": [{"type": "text", "data": {"text": "我刚才说了我叫什么？"}}],
                "raw_message": "我刚才说了我叫什么？",
            }
        )

        steps = [
            ("私聊第1句", private_event, True, None),
            ("群聊未@且无触发词", group_event, False, None),
            ("群聊@机器人", group_at_event, True, "at"),
            ("群聊触发词开头", keyword_event, True, None),
            ("私聊第2句（应带历史）", second_private, True, "history"),
        ]
        outputs = []
        for label, event, expect_reply, check in steps:
            await ws.send(json.dumps(event, ensure_ascii=False))
            sent = await _wait_send(ws)
            text = ""
            if sent:
                for seg in sent.get("params", {}).get("message", []):
                    if isinstance(seg, dict) and seg.get("type") == "text":
                        text += seg.get("data", {}).get("text", "")
                outputs.append(
                    {
                        "scene": label,
                        "expect_reply": expect_reply,
                        "reply_text": text,
                        "message_type": sent.get("params", {}).get("message_type"),
                        "group_id": sent.get("params", {}).get("group_id"),
                    }
                )
            else:
                outputs.append({"scene": label, "expect_reply": expect_reply, "got": None})
            if bool(sent) != expect_reply:
                RESULT["error"] = f"{label} 行为不符合预期: {sent}"
                RESULT["steps"] = outputs
                return
            if check == "at" and sent:
                segs = sent.get("params", {}).get("message", [])
                has_at = any(s.get("type") == "at" for s in segs if isinstance(s, dict))
                if not has_at:
                    RESULT["error"] = f"{label} 的群回复没有 @ 提问者: {segs}"
                    RESULT["steps"] = outputs
                    return
            if check == "history" and "带历史0条" in text:
                RESULT["error"] = f"{label} 没有带上上下文历史: {text}"
                RESULT["steps"] = outputs
                return
        RESULT["ok"] = True
        RESULT["steps"] = outputs


async def _wait_send(ws, timeout: float = 10.0):
    """等待并回应协议端请求，返回本次是否发出了消息（及内容）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=deadline - time.time())
        except asyncio.TimeoutError:
            return None  # 预期不回消息的场景
        data = json.loads(raw)
        action = data.get("action")
        print("[fake-napcat] recv:", json.dumps(data, ensure_ascii=False)[:200])
        # OneBot v11 协议端必须对每个 API 调用回带 echo 的响应
        await ws.send(
            json.dumps(
                {"status": "ok", "retcode": 0, "data": {"message_id": 99}, "echo": data.get("echo")},
                ensure_ascii=False,
            )
        )
        if action in ("send_private_msg", "send_group_msg", "send_msg"):
            return data
    return None


def main():
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    try:
        asyncio.run(fake_napcat())
    except Exception as exc:  # noqa: BLE001
        RESULT["error"] = f"{type(exc).__name__}: {exc}"

    print("\n================ E2E 结果 ================")
    if RESULT.get("ok"):
        print("✅ 链路打通：模拟 NapCat → NoneBot → 大肥鱼插件 → 回发消息")
        print(json.dumps(RESULT["steps"], ensure_ascii=False, indent=2, default=str)[:2000])
        sys.exit(0)
    print("❌ 失败：", RESULT.get("error", "未知"))
    sys.exit(1)


if __name__ == "__main__":
    main()
