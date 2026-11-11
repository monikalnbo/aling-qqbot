"""NoneBot2 插件入口：接收 OneBot v11 私聊 / 群聊消息，交给 ReplyEngine 处理。

触发规则
- 私聊：任何非空消息都会回复
- 群聊：@机器人，或消息以触发词（默认「大肥鱼」）开头
- 群聊采用“一人一上下文”，不同群友互不串记忆
"""

from __future__ import annotations

from nonebot import get_driver, logger, on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    MessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.plugin import PluginMetadata

from .app import DafeiyuApp, create_app
from .config import DafeiyuConfig
from .memory import MemoryStore

__plugin_meta__ = PluginMetadata(
    name="大肥鱼",
    description="接入 DeepSeek 的 QQ 聊天机器人「大肥鱼」",
    usage="私聊直接说话；群聊 @大肥鱼 或消息以「大肥鱼」开头",
    type="application",
    config=DafeiyuConfig,
)

APP: DafeiyuApp = create_app()

if not APP.config.deepseek_api_key:
    logger.warning(
        "未配置 DAFEIYU_DEEPSEEK_API_KEY：大肥鱼会回复错误提示。请在 .env 中填入 DeepSeek Key"
    )

matcher = on_message(priority=10, block=True)


def _session_key(event: MessageEvent) -> str:
    if isinstance(event, GroupMessageEvent):
        return MemoryStore.group_key(str(event.group_id), str(event.user_id))
    return MemoryStore.private_key(str(event.user_id))


def _nickname(event: MessageEvent) -> str:
    sender = getattr(event, "sender", None)
    return getattr(sender, "card", "") or getattr(sender, "nickname", "") or ""


async def _typing(bot: Bot, event: MessageEvent) -> None:
    if not APP.config.show_typing:
        return
    try:
        if isinstance(event, GroupMessageEvent):
            await bot.call_api(
                "set_group_typing_status", group_id=event.group_id, is_typing=True
            )
        else:
            await bot.call_api("set_input_status", user_id=event.user_id, event_type=1)
    except Exception:  # 状态接口并非所有协议端都实现，失败忽略
        pass


@matcher.handle()
async def handle_message(bot: Bot, event: MessageEvent) -> None:
    text = event.get_plaintext().strip()
    if not text:
        return

    if isinstance(event, GroupMessageEvent):
        # is_tome() 由适配器按 CQ:at 与机器人自身 QQ 号判断
        if not APP.engine.group_triggered(text, at_me=event.is_tome()):
            return
        scene = "group"
    elif isinstance(event, PrivateMessageEvent):
        scene = "private"
    else:
        return

    await _typing(bot, event)
    answer = await APP.engine.reply(
        session_key=_session_key(event),
        text=text,
        scene=scene,
        nickname=_nickname(event),
    )

    if isinstance(event, GroupMessageEvent):
        await bot.send(event, MessageSegment.at(event.user_id) + " " + answer)
    else:
        await bot.send(event, answer)


@get_driver().on_shutdown
async def _shutdown() -> None:
    await APP.client.close()
