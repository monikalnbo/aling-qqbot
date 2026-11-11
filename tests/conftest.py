"""测试公共准备。

- 先 nonebot.init()，因为插件模块导入时会读 NoneBot 配置
- 不使用 nonebug 的 session 级 fixture，避免与本地 pytest-asyncio 版本冲突，
  直接构造 OneBot v11 事件 + 假 Bot 驱动插件处理函数
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import os  # noqa: E402

os.environ.setdefault("DAFEIYU_DEEPSEEK_API_KEY", "sk-test-not-real")

import nonebot  # noqa: E402

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(driver="~none")

from nonebot.adapters.onebot.v11 import Adapter, Bot  # noqa: E402

nonebot.get_driver().register_adapter(Adapter)

SELF_ID = 10000


class FakeClient:
    """记录调用参数的假 DeepSeek 客户端。"""

    def __init__(self, replies=None):
        self.calls = []
        self.replies = list(replies or ["咕噜～鱼在呢"])

    async def chat(self, messages):
        self.calls.append(messages)
        if len(self.replies) > 1:
            return self.replies.pop(0)
        return self.replies[0]

    async def close(self):
        return None


class FakeBot(Bot):
    """只记录 send / call_api，不发真实请求。"""

    def __init__(self):
        super().__init__(Adapter(nonebot.get_driver()), str(SELF_ID))
        self.sent = []
        self.api_calls = []

    async def send(self, event, message=None, **kwargs):
        self.sent.append(message)

    async def call_api(self, api, **data):
        self.api_calls.append((api, data))
        return None


def private_event(user_id=123, text="你好"):
    from nonebot.adapters.onebot.v11 import PrivateMessageEvent

    return PrivateMessageEvent(
        time=1700000000,
        self_id=SELF_ID,
        post_type="message",
        sub_type="friend",
        user_id=user_id,
        message_type="private",
        message_id=1,
        message=[{"type": "text", "data": {"text": text}}],
        original_message=[],
        raw_message=text,
        font=0,
        sender={"user_id": user_id, "nickname": "小明"},
        to_me=True,
    )


def group_event(user_id=777, group_id=555, text="大肥鱼 你好", at_bot=False):
    from nonebot.adapters.onebot.v11 import GroupMessageEvent

    message = []
    if at_bot:
        message.append({"type": "at", "data": {"qq": str(SELF_ID)}})
    message.append({"type": "text", "data": {"text": text}})
    return GroupMessageEvent(
        time=1700000000,
        self_id=SELF_ID,
        post_type="message",
        sub_type="normal",
        group_id=group_id,
        user_id=user_id,
        message_type="group",
        message_id=2,
        message=message,
        original_message=[],
        raw_message=text,
        font=0,
        sender={"user_id": user_id, "nickname": "小红", "card": ""},
        to_me=at_bot,
    )


@pytest.fixture(scope="session")
def plugin():
    return nonebot.load_plugin("dafeiyu")
