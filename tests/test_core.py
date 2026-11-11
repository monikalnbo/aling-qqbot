import pytest

from dafeiyu.config import DafeiyuConfig
from dafeiyu.core import FALLBACK_PERSONA, ReplyEngine, load_persona, strip_at
from dafeiyu.memory import MemoryStore
from tests.conftest import FakeClient


def build_engine(client=None, **overrides):
    conf = DafeiyuConfig(**overrides)
    store = MemoryStore(max_turns=conf.max_history_turns)
    client = client or FakeClient(["噗噜噜，鱼在听着呢"])
    return ReplyEngine(conf, store, client), store, client


def test_strip_at():
    assert strip_at("[CQ:at,qq=123456] 大肥鱼 你好") == "大肥鱼 你好"
    assert strip_at("@大肥鱼 你好") == "你好"
    assert strip_at("你好 [CQ:at,qq=42]") == "你好"


def test_group_trigger():
    engine, *_ = build_engine()
    assert engine.group_triggered("随便聊聊", at_me=True) is True
    assert engine.group_triggered("大肥鱼今天吃了吗", at_me=False) is True
    assert engine.group_triggered("[CQ:at,qq=1] 大肥鱼在吗", at_me=False) is True
    assert engine.group_triggered("今天天气不错", at_me=False) is False


def test_prepare_text_removes_trigger_word():
    engine, *_ = build_engine()
    assert engine.prepare_text("[CQ:at,qq=9] 大肥鱼 讲个笑话") == "讲个笑话"
    assert engine.prepare_text("大肥鱼：你好") == "你好"
    assert engine.prepare_text("今天吃什么") == "今天吃什么"


def test_persona_priority(tmp_path):
    f = tmp_path / "p.txt"
    f.write_text("文件人设", encoding="utf-8")
    assert load_persona(DafeiyuConfig(persona_file=str(f)), tmp_path) == "文件人设"
    assert (
        load_persona(
            DafeiyuConfig(persona_file=str(f), persona_inline="内联人设"), tmp_path
        )
        == "内联人设"
    )
    missing = DafeiyuConfig(persona_file=str(tmp_path / "nope.txt"))
    assert load_persona(missing, tmp_path) == FALLBACK_PERSONA


def test_system_prompt_contains_persona_and_scene():
    engine, *_ = build_engine(persona_inline="你是大肥鱼")
    sp = engine.build_system_prompt(scene="group", nickname="阿明")
    assert "大肥鱼" in sp
    assert "群聊" in sp
    assert "阿明" in sp


@pytest.mark.asyncio
async def test_private_reply_and_memory_roundtrip():
    engine, store, client = build_engine(FakeClient(["第一句", "第二句"]))
    key = MemoryStore.private_key("1")
    r1 = await engine.reply(session_key=key, text="我叫小明", scene="private", nickname="小明")
    assert r1 == "第一句"
    r2 = await engine.reply(session_key=key, text="我叫啥", scene="private")
    assert r2 == "第二句"

    second_messages = client.calls[1]
    assert [m["role"] for m in second_messages] == ["system", "user", "assistant", "user"]
    assert second_messages[1]["content"] == "我叫小明"
    assert second_messages[3]["content"] == "我叫啥"


@pytest.mark.asyncio
async def test_reset_keyword_clears_memory():
    engine, store, client = build_engine()
    key = MemoryStore.group_key("100", "1")
    store.append(key, "user", "旧话题")
    out = await engine.reply(session_key=key, text="重置", scene="group")
    assert "忘掉" in out
    assert store.history(key) == []
    assert client.calls == []


@pytest.mark.asyncio
async def test_help_keyword():
    engine, _, client = build_engine()
    out = await engine.reply(session_key="k", text="帮助", scene="private")
    assert "私聊" in out and "群聊" in out
    assert client.calls == []


@pytest.mark.asyncio
async def test_deepseek_error_is_friendly():
    class Boom:
        async def chat(self, messages):
            from dafeiyu.llm import DeepSeekError

            raise DeepSeekError("连接 DeepSeek 失败: timeout")

    engine, store, _ = build_engine(Boom())
    out = await engine.reply(session_key="k", text="你好", scene="private")
    assert "连不上大脑" in out
    # 失败不入记忆，避免把错误当上下文
    assert store.history("k") == []


@pytest.mark.asyncio
async def test_long_reply_truncated():
    engine, *_ = build_engine(FakeClient(["啊" * 5000]), max_reply_chars=100)
    out = await engine.reply(session_key="k", text="说长点", scene="private")
    assert len(out) <= 101
    assert out.endswith("…")


@pytest.mark.asyncio
async def test_blank_after_trigger_word():
    engine, _, client = build_engine()
    out = await engine.reply(session_key="k", text="大肥鱼", scene="group")
    assert "你说吧" in out
    assert client.calls == []
