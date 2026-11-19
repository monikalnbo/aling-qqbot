"""cly · 托管守卫插件

为什么有它
----------
cly 是托管给别人的机器人，必须**只用他自己的 DeepSeek key**。
没填 key 之前，最怕两件事：

1. 拿我们的 key 去回答（花钱、且客人以为是自己的）；
2. 调接口失败，把一大串报错滚屏发出去。

这个插件在 AstrBot「准备调用大模型之前」那一步（`on_llm_request` 钩子）检查：
key 没填 / 还是占位符 / 明显不是 sk- 开头 → 直接把这一轮停掉，回一句人话，
一次网络请求都不发。

检查的 key 来源（按顺序，取第一个非空）
--------------------------------------
1. 环境变量 `ASTROBOT_CLY_DEEPSEEK_KEY`（可选，运维临时测试用）
2. `data/astrbot/cmd_config.json` 里的 `provider[0].key[0]`
   —— 自助设置页保存时会把他的 key 写到这里（明文，权限 600）

同时会校验 key 不能是 DeepSeek 官方模板里的占位符（`sk-xxx` / `sk-...`），
避免「填了个样例就以为配好了」。

另外提供一个管理员口令 `/cly配置`，让人在 QQ 里自查当前填没填（不显示 key）。
"""

from __future__ import annotations

import json
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register

PLUGIN_NAME = "astrbot_plugin_cly_guard"
PLUGIN_VERSION = "1.0.0"

# AstrBot 内部「这一轮别调模型、把这句话发给用户」用的 extra 键
LLM_ERROR_MESSAGE_EXTRA_KEY = "_llm_error_message"
# 标在 event 上，告诉语音插件「这轮是守卫插件的提示语，别合成语音」
GUARD_NOTICE_EXTRA = "cly_guard_notice"

NO_KEY_NOTICE = (
    "这个机器人还没配置自己的 key，现在还不能正常聊天。\n"
    "请让管理员到设置页填一下 DeepSeek Key 和模型，保存后就能用了。\n"
    "（没填之前我不会调用任何付费接口，放心。）"
)

BAD_KEY_NOTICE = (
    "设置页里的 DeepSeek Key 不像是一个真的 key（应该是 sk- 开头的长串）。\n"
    "请让管理员到设置页重新填一次，保存后就能用了。"
)

# 设置工具在「没填 key」时写进 provider 配置的占位值（见 cly/tools/cly_settings.py）
PLACEHOLDER_KEY = "sk-cly-not-configured-placeholder"

PLACEHOLDERS = {
    "",
    PLACEHOLDER_KEY,
    "sk-xxx",
    "sk-...",
    "sk-xxxx",
    "你的key",
    "填入你的key",
    "none",
    "null",
}


def _read_provider_key(data_dir: Path) -> str:
    """读 cly 自己的 provider key（只读，不改）。"""
    conf_path = data_dir / "cmd_config.json"
    try:
        raw = conf_path.read_bytes()
        conf = json.loads(raw.decode("utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[cly-guard] 读不到 cmd_config.json：{exc}")
        return ""
    for p in conf.get("provider") or []:
        if not isinstance(p, dict):
            continue
        keys = p.get("key")
        if isinstance(keys, list) and keys:
            first = str(keys[0] or "").strip()
            if first:
                return first
        elif isinstance(keys, str) and keys.strip():
            return keys.strip()
    return ""


def _looks_like_real_key(key: str) -> bool:
    k = (key or "").strip()
    if k == PLACEHOLDER_KEY or k.lower() in PLACEHOLDERS:
        return False
    if not k.startswith("sk-"):
        return False
    return len(k) >= 20  # DeepSeek 的 key 都是 sk- 加 32 位左右


@register(
    PLUGIN_NAME,
    "cly",
    "托管守卫：没填他自己的 key 就不调付费接口，只回友好提示",
    PLUGIN_VERSION,
)
class ClyGuardPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        # AstrBot 的 data 目录（容器里是 /AstrBot/data）。StarTools 落到
        # data/plugin_data/<插件名>/，上两级就是 data/。
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME)).parents[1]

    def _current_key(self) -> str:
        # 只认 cly 自己的配置文件。故意不读环境变量：托管机上的环境变量是
        # 我们自己的 key，读了就等于「客人没填、我们替它付钱」。
        return _read_provider_key(self.data_dir)

    def _key_ok(self) -> bool:
        return _looks_like_real_key(self._current_key())

    async def _block(self, event: AstrMessageEvent, notice: str) -> None:
        """停掉这一轮：标记 + 把提示语交给 AstrBot 直接发出。"""
        event.set_extra(GUARD_NOTICE_EXTRA, notice)
        event.set_extra(LLM_ERROR_MESSAGE_EXTRA_KEY, notice)
        await event.send(MessageChain([Plain(notice)]))
        event.stop_event()
        logger.info(
            f"[cly-guard] 拦下一轮 LLM 调用（未配置有效 key）umo={event.unified_msg_origin}"
        )

    @filter.on_llm_request()
    async def guard_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """调模型之前检查 key；没有就立刻停，一个付费请求都不发。"""
        key = self._current_key()
        # 没填时设置工具会写占位值进去；占位值等于「没填」，要回「还没配置」
        # 而不是「格式不对」，否则主人第一次用会以为是 key 填错了。
        if not key or key == PLACEHOLDER_KEY:
            await self._block(event, NO_KEY_NOTICE)
            return
        if not _looks_like_real_key(key):
            await self._block(event, BAD_KEY_NOTICE)

    # ---------------- 语音：没填 key / key 用不了，就别合成 ----------------
    def _voice_key(self) -> str:
        """读 cly 自己的语音配置里的 key（语音插件与设置页写的是同一个文件）。"""
        conf = self.data_dir / "config" / "astrbot_plugin_aling_voice_config.json"
        try:
            data = json.loads(conf.read_text(encoding="utf-8-sig"))
        except Exception:
            return ""
        return str(data.get("api_key") or "").strip()

    def _voice_status(self) -> dict:
        """设置页保存时留下的检测结果：voice_ok / msg。"""
        f = self.data_dir / "cli_settings" / "voice_status.json"
        try:
            data = json.loads(f.read_text(encoding="utf-8-sig"))
            return {"ok": bool(data.get("voice_ok")), "msg": str(data.get("msg") or "")}
        except Exception:
            return {"ok": False, "msg": "还没检测过"}

    def _voice_usable(self) -> tuple[bool, str]:
        key = self._voice_key()
        if not key:
            return False, "语音 Key 未填写"
        st = self._voice_status()
        if not st["ok"]:
            return False, st["msg"] or "语音 Key 检测未通过"
        return True, "ok"

    @filter.on_decorating_result()
    async def guard_voice(self, event: AstrMessageEvent):
        """语音 key 没填或检测不通过时，把语音回复改回文字，一次合成都不会发。

        这是最后一道闸：即使语音插件那边因为配置没刷新而想合成，
        这里也会先把音频段拿掉，保证「没填 key 不花钱」。
        """
        if event.get_extra(GUARD_NOTICE_EXTRA):
            return  # 守卫自己的提示语本来就是文字
        usable, why = self._voice_usable()
        if usable:
            return
        result = event.get_result()
        if result is None or not getattr(result, "chain", None):
            return
        has_audio = any(getattr(c, "type", "") in ("Record", "record") for c in result.chain)
        if not has_audio:
            return
        new_chain = []
        for c in result.chain:
            if getattr(c, "type", "") in ("Record", "record"):
                # Record 上带着原文字（text 属性），退回去发文字
                txt = getattr(c, "text", "") or ""
                if txt:
                    new_chain.append(Plain(txt))
            else:
                new_chain.append(c)
        result.chain = new_chain
        logger.info(f"[cly-guard] 语音不可用（{why}），已把语音改回文字，未调用语音接口")

    @filter.command("cly配置")
    async def cmd_status(self, event: AstrMessageEvent):
        """ /cly配置 —— 自查这台机器人有没有填自己的 key（不显示 key 内容）"""
        key = self._current_key()
        if not key:
            state = "❌ DeepSeek Key：未填写"
        elif _looks_like_real_key(key):
            state = f"✅ DeepSeek Key：已填写（尾号 {key[-4:]}）"
        else:
            state = "⚠️ DeepSeek Key：填了，但格式不对（应 sk- 开头）"
        vkey = self._voice_key()
        if not vkey:
            vstate = "❌ 语音 Key：未填写（只发文字）"
        else:
            vok, vwhy = self._voice_usable()
            vstate = (
                f"✅ 语音 Key：可用（尾号 {vkey[-4:]}）"
                if vok
                else f"⚠️ 语音 Key：不可用（{vwhy}），先只发文字"
            )
        yield event.plain_result(
            f"{state}\n{vstate}\n"
            "以上只是自查，修改请到设置页（只有管理员知道网址）。"
        )
