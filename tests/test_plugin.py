"""插件级测试：模拟 OneBot v11 事件，验证触发、回复、@ 与上下文隔离。"""

import pytest

from tests.conftest import FakeBot, FakeClient, group_event, private_event


@pytest.fixture
def mod(plugin):
    """插件模块本体；多个测试模块各自 load 一次会返回 None，此时直接取已导入的模块。"""
    import sys

    return plugin.module if plugin is not None else sys.modules["dafeiyu"]


@pytest.fixture
def bot_and_client(mod):
    client = FakeClient(["在呢，我是大肥鱼"])
    mod.APP.client = client
    mod.APP.engine.client = client  # ReplyEngine 在构造时持有引用，测试需同步替换
    mod.APP.store.clear()
    return FakeBot(), client


async def _dispatch(mod, bot, event):
    await mod.handle_message(bot, event)


@pytest.mark.asyncio
async def test_private_message_replies(bot_and_client, mod):
    bot, client = bot_and_client
    await _dispatch(mod, bot, private_event(text="你好呀"))
    assert len(client.calls) == 1
    assert client.calls[0][-1]["content"] == "你好呀"
    assert bot.sent and "大肥鱼" in str(bot.sent[0])


@pytest.mark.asyncio
async def test_group_message_without_at_is_ignored(bot_and_client, mod):
    bot, client = bot_and_client
    await _dispatch(mod, bot, group_event(text="今天天气不错", at_bot=False))
    assert client.calls == []
    assert bot.sent == []


@pytest.mark.asyncio
async def test_group_at_bot_replies_with_at(bot_and_client, mod):
    bot, client = bot_and_client
    await _dispatch(mod, bot, group_event(text=" 讲个笑话", at_bot=True))
    assert len(client.calls) == 1
    assert len(bot.sent) == 1
    assert "at" in str(bot.sent[0]) or "大肥鱼" in str(bot.sent[0])


@pytest.mark.asyncio
async def test_group_prefix_keyword_triggers(bot_and_client, mod):
    bot, client = bot_and_client
    await _dispatch(mod, bot, group_event(text="大肥鱼 你好", at_bot=False))
    assert len(client.calls) == 1
    # 触发词已从问题里剥掉
    assert client.calls[0][-1]["content"] == "你好"


@pytest.mark.asyncio
async def test_two_users_in_same_group_have_separate_memory(bot_and_client, mod):
    bot, client = bot_and_client
    mod.APP.store.clear()
    await _dispatch(mod, bot, group_event(user_id=1, group_id=9, text="大肥鱼 我叫甲", at_bot=False))
    await _dispatch(mod, bot, group_event(user_id=2, group_id=9, text="大肥鱼 我叫乙", at_bot=False))

    # 第二个人发起请求时，上下文里不应出现第一个人的话
    second_payload = " ".join(m["content"] for m in client.calls[1])
    assert "我叫甲" not in second_payload
    assert "我叫乙" in second_payload
    # 第三个人也不会带上前两人的历史
    await _dispatch(mod, bot, group_event(user_id=3, group_id=9, text="大肥鱼 在吗", at_bot=False))
    third_payload = " ".join(m["content"] for m in client.calls[2])
    assert "我叫甲" not in third_payload and "我叫乙" not in third_payload
