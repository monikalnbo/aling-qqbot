"""对话核心逻辑：触发判断、触发词剥离、人设拼装、多轮记忆读写。

这里不依赖 NoneBot 的运行时，方便单独做单元测试。
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import DafeiyuConfig
from .llm import DeepSeekClient, DeepSeekError
from .memory import MemoryStore

CQ_AT_PREFIX = re.compile(r"^\[CQ:at,qq=\d+\]\s*")
CQ_AT_SUFFIX = re.compile(r"\s*\[CQ:at,qq=\d+\]$")

FALLBACK_PERSONA = (
    "你叫「大肥鱼」，是一只住在 QQ 里的蓝色鲸鱼娘，性格黏人、嘴碎、有点小傲娇，"
    "自称「鱼」或者「本鱼」，管对方叫「你」。说话口语化、简短（一般 1~3 句），"
    "偶尔带「噗噜噜」「咕噜」这类语气词，不要用 Markdown，不要输出列表。"
)


def load_persona(config: DafeiyuConfig, base_dir: Path | None = None) -> str:
    """人设优先顺序：inline > persona_file > 内置默认。"""
    if config.persona_inline.strip():
        return config.persona_inline.strip()
    root = base_dir or Path.cwd()
    path = Path(config.persona_file)
    if not path.is_absolute():
        path = root / path
    if path.is_file():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    return FALLBACK_PERSONA


def strip_at(text: str) -> str:
    """去掉消息开头的 @（CQ 码或纯文本 @名字）。"""
    text = CQ_AT_PREFIX.sub("", text.strip())
    text = CQ_AT_SUFFIX.sub("", text.strip())
    # 有些适配器把 @ 渲染成 "@大肥鱼 " 这样的纯文本
    text = re.sub(r"^@\S+\s+", "", text.strip())
    return text.strip()


class ReplyEngine:
    def __init__(
        self,
        config: DafeiyuConfig,
        store: MemoryStore,
        client: DeepSeekClient,
        *,
        persona: str | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.client = client
        self.persona = persona or load_persona(config)

    # ---------- 触发判断 ----------
    def _keyword_hit(self, text: str) -> bool:
        clean = strip_at(text)
        return any(clean.startswith(k) for k in self.config.group_trigger_keywords if k)

    def group_triggered(self, text: str, *, at_me: bool) -> bool:
        if at_me:
            return True
        return self._keyword_hit(text)

    def private_triggered(self, text: str) -> bool:
        return bool(strip_at(text))

    # ---------- 指令 ----------
    def is_reset(self, text: str) -> bool:
        clean = strip_at(text)
        return clean in self.config.reset_keywords

    def is_help(self, text: str) -> bool:
        clean = strip_at(text)
        return clean in self.config.help_keywords

    def help_text(self) -> str:
        name = self.config.nickname[0] if self.config.nickname else "大肥鱼"
        kw = "、".join(self.config.group_trigger_keywords)
        return (
            f"我是{name}～\n"
            "· 私聊：直接发消息就能聊\n"
            f"· 群聊：@我，或者消息以「{kw}」开头\n"
            f"· 发「{self.config.reset_keywords[0]}」清空当前对话记忆\n"
            "· 会记住最近几轮聊天，换话题也可以直接说"
        )

    # ---------- 人设拼装 ----------
    def build_system_prompt(self, *, scene: str, nickname: str = "") -> str:
        scene_text = (
            "现在是 QQ 群聊，对方在群里 @ 了你，回复要适合群聊、别太长。"
            if scene == "group"
            else "现在是 QQ 私聊，一对一聊天。"
        )
        extra = f"\n对方 QQ 昵称：{nickname}。" if nickname else ""
        return f"{self.persona}\n\n{scene_text}{extra}\n始终保持「大肥鱼」的身份，不要暴露自己是 AI 模型。"

    # ---------- 主流程 ----------
    def prepare_text(self, text: str) -> str:
        clean = strip_at(text)
        for kw in self.config.group_trigger_keywords:
            if kw and clean.startswith(kw):
                clean = clean[len(kw) :].lstrip(" ，,：:、～~")
                break
        return clean.strip()

    async def reply(
        self,
        *,
        session_key: str,
        text: str,
        scene: str,
        nickname: str = "",
    ) -> str:
        """生成回复并写入记忆；异常时返回给用户的友好提示。"""
        clean = self.prepare_text(text)
        if not clean:
            return f"在呢～{self.config.nickname[0] if self.config.nickname else '大肥鱼'}听着，你说吧。"

        if self.is_reset(clean):
            self.store.reset(session_key)
            return "好哦，刚才聊的鱼都忘掉啦，重新开始吧～"

        if self.is_help(clean):
            return self.help_text()

        history = self.store.history(session_key)
        messages = DeepSeekClient.build_messages(
            self.build_system_prompt(scene=scene, nickname=nickname), history, clean
        )
        try:
            answer = await self.client.chat(messages)
        except DeepSeekError as exc:
            return f"鱼这边连不上大脑了…（{exc}）"
        except Exception as exc:  # 防御：任何意外都不让 bot 崩
            return f"鱼刚才脑袋抽筋了…（{type(exc).__name__}: {exc}）"

        answer = (answer or "").strip()
        if not answer:
            answer = "咕噜……鱼一下子没想出来说啥。"
        if len(answer) > self.config.max_reply_chars:
            answer = answer[: self.config.max_reply_chars].rstrip() + "…"

        # 先写用户消息再写回复，保证顺序
        self.store.append(session_key, "user", clean)
        self.store.append(session_key, "assistant", answer)
        return answer
