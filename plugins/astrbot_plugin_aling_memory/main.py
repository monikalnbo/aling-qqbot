"""阿凌 · 三层记忆插件（AstrBot 4.29.x）

三层结构
--------
群聊存档：`on_group_message` 监听群内每条消息（**包括没 @ 阿凌的**），原样写进
      本地 group_messages 表。只记不打扰：不触发回复、不额外调模型、不发消息。

短期：`on_llm_request` 里把 `req.contexts` 裁到最近 N 轮（整轮裁剪，保留 system）。
      上下文条数恒定封顶，成本不随聊天时长增长。
回看：只在「被问到过去的事 / 判断信息不足」时，才去 group_messages 里按关键词
      或最近时间取几段原文塞进当次请求；日常请求不查库，不涨成本。
中期：被裁掉的旧轮 + 空闲超时的旧轮，交给 LLM 压成一段摘要，按会话（群号/私聊QQ号）
      存进 session_summaries，下次请求时以「会话摘要」块注入。
长期：每 N 轮让 LLM 从最近对话里抽取关键事实（称呼/偏好/约定/干过的事），按
      (会话, QQ号) 归档进 user_facts；每次请求按最后一条消息的关键词重合度检索，
      只注入「当前说话人自己」的条目（默认 owner_only，不会串）。

身份锚点
--------
在 AstrBot 自带 `User ID/Nickname` 之外，追加一行 unified_id
（平台:消息类型:会话:QQ号），把平台 user_id 与 unified_id 对齐，昵称只作显示名。

调试命令：`/mem status`、`/mem facts [QQ号]`、`/mem summary`、`/mem fact <文本>`、
`/mem forget [QQ号]`、`/mem recent`（本群最近 10 条存档）、`/mem log <关键词>`（手动回看）
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.agent.message import TextPart

from .store import MemoryStore

PLUGIN_NAME = "astrbot_plugin_aling_memory"

DEFAULT_CONFIG: dict[str, Any] = {
    "enable_identity": True,
    "short_term_max_turns": 12,
    "mid_term_enable": True,
    "mid_term_keep_turns": 6,
    "mid_term_idle_seconds": 1800,
    "long_term_enable": True,
    "long_term_extract_every_turns": 1,
    "long_term_top_k": 6,
    "long_term_scope": "owner_only",
    "reply_strip_qq": True,
    "group_archive_enable": True,
    "group_archive_max_rows": 20000,
    "group_archive_store_bot": True,
    "recall_enable": True,
    "recall_top_k": 8,
    "recall_scan": 2000,
    "debug_log": True,
}

SUMMARY_HEADER = "【会话摘要（本地记忆插件生成，覆盖更早的对话）】"
FACT_HEADER = "【长期记忆：{qq} 本人相关档案】"
IDENTITY_START = "<system_reminder>"

# 兜底：模型偶尔仍会把内部编号念出来（如「一串数字 你刚说什么」）。
# 只处理完整的 5 位以上数字串：数字串后面直接跟中文/标点时补一个空格再删，
# 绝不吞掉正常标点；15 位以上的长数字（如天气时间戳）保持原样，避免误伤。
_QQ_RUN_RE = re.compile(r"(?<![0-9])([0-9]{5,14})(?![0-9])")
_CN_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")

ARCHIVE_HEADER = "【群里刚才的聊天记录（本地存档摘录，按时间正序）】"

# 「回看」触发词：只有问过去的事、或者明说自己不知道时才查库。
# 日常「在吗」「吃了吗」这类不该命中，保证日常请求不查库、不加成本。
RECALL_TRIGGER_RE = re.compile(
    r"(刚才|刚刚|刚说|刚聊|之前|以前|上次|上回|上一条|昨天|前天|昨晚|今早|早上|上午|中午|下午|晚上|"
    r"这几天|最近|半天|一会儿前|"
    r"谁说的|谁说了|谁说|哪个说的|谁提到|谁在说|说的是谁|"
    r"聊了什么|聊了啥|说了什么|说了啥|讲了什么|讲了啥|发生了什么|发生什么|怎么回事|什么情况|"
    r"记得|记不记得|还记得|想不起来|不记得|"
    r"历史|记录|翻旧账|回看|查一下|帮我查|找一下|搜一下|"
    r"at 我|艾特我|叫我了)"
)

# 泛问：问的是「刚才/那群人/大家」的整体情况，而不是某个具体话题。
# 这类问题只按关键词取几条会把别的说话人漏掉，要再补一段最近原文。
RECALL_BROAD_RE = re.compile(
    r"(群里|群聊|大家|都聊了什么|都说了什么|聊了什么|聊了啥|说了什么|说了啥|讲了什么|讲了啥|"
    r"发生了什么|发生什么|怎么回事|什么情况|谁说了什么|谁说了啥|说了些什么)"
)

SUMMARY_PROMPT = (
    "把下面这段群聊/私聊记录压缩成一段中文摘要，供之后的对话继续使用。\n"
    "要求：\n"
    "1. 保留人物与身份的对应关系：每条信息都标清楚是哪个 QQ 号（以及当时的昵称）说的、做过什么，不要张冠李戴；\n"
    "2. 保留话题、结论、约定、待办和情绪走向，删掉寒暄与重复；\n"
    "3. 300 字以内，纯文本，不要标题、不要列表符号以外的多余格式；\n"
    "4. 如果没有值得保留的信息，就输出“（无重要内容）”。\n"
    "已有摘要（如果有，合并进去，不要丢掉其中仍然有效的信息）：\n{prev}\n\n"
    "需要压缩的对话：\n{body}"
)

FACT_PROMPT = (
    "你是记忆整理器。下面是最近几轮对话，请只提取「{qq}」这个人本人的、"
    "以后还可能被提起的长期事实。\n"
    "只提取这四类：\n"
    "- 称呼：他明确说过自己叫什么/希望被怎么叫\n"
    "- 偏好：明确说喜欢或讨厌什么（食物、事物、习惯）\n"
    "- 约定：和对方约好的时间、事情\n"
    "- 干过的事：他自己明确说发生过、以后会被追问的具体经历\n"
    "硬性要求：\n"
    "1. 只用「{qq}」自己说的话，不要写别人的话，不要写阿凌的反应和吐槽；\n"
    "2. 不要写昵称、群号、群成员身份这类无信息量的内容；\n"
    "3. 不要写寒暄、提问、情绪抱怨、一次性的天气闲聊；\n"
    "4. 如果这句话本身是个问题（问别人、问自己），就不算事实；\n"
    "5. 宁缺毋滥，没有值得记的就只输出：无。\n"
    "每条一行，以「- 」开头，一条不超过 40 字，最多 3 条。\n\n"
    "对话：\n{body}"
)


@register(
    "astrbot_plugin_aling_memory",
    "aling",
    "按 QQ 号归档的三层记忆 + 群聊全量存档与按需回看，并补充 unified_id 身份锚点",
    "1.1.0",
)
class AlingMemoryPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        cfg = dict(DEFAULT_CONFIG)
        if isinstance(config, dict):
            cfg.update({k: v for k, v in config.items() if v is not None})
        self.cfg = cfg
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.data_dir = data_dir
        self.store = MemoryStore(data_dir / "memory.db")
        self._inflight: set[str] = set()

    async def initialize(self) -> None:
        self._last_umo = ""
        self.log(
            f"初始化完成 dir={self.data_dir} max_turns={self.cfg['short_term_max_turns']} "
            f"mid={self.cfg['mid_term_enable']} long={self.cfg['long_term_enable']} "
            f"scope={self.cfg['long_term_scope']} facts={self.store.count_facts()} "
            f"archive={self.cfg.get('group_archive_enable', True)} "
            f"archived={self.store.count_group_messages()} "
            f"recall={self.cfg.get('recall_enable', True)}"
        )

    async def terminate(self) -> None:
        self.store.close()

    # ------------------------------------------------------------------ 工具
    def log(self, msg: str) -> None:
        if self.cfg.get("debug_log", True):
            logger.info(f"[aling-memory] {msg}")

    @staticmethod
    def _text_of(item: Any) -> str:
        if isinstance(item, dict):
            c = item.get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                return "".join(
                    p.get("text", "") if isinstance(p, dict) else str(p) for p in c
                )
            return ""
        c = getattr(item, "content", "")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "".join(
                p.get("text", "") if isinstance(p, dict) else str(p) for p in c
            )
        return ""

    @staticmethod
    def _role_of(item: Any) -> str:
        if isinstance(item, dict):
            return str(item.get("role", ""))
        return str(getattr(item, "role", ""))

    def _split_history(self, contexts: list[Any]) -> tuple[list[Any], list[Any]]:
        """拆成 (system 前置部分, 对话部分)。"""
        idx = 0
        for i, item in enumerate(contexts):
            if self._role_of(item) != "system":
                idx = i
                break
        else:
            return contexts, []
        return contexts[:idx], contexts[idx:]

    @staticmethod
    def _group_into_rounds(dialogue: list[Any]) -> list[list[Any]]:
        """以 user 消息开头切轮，保证 user/assistant 不拆散。"""
        rounds: list[list[Any]] = []
        cur: list[Any] = []
        for item in dialogue:
            role = AlingMemoryPlugin._role_of(item)
            if role == "user" and cur:
                rounds.append(cur)
                cur = [item]
            else:
                cur.append(item)
        if cur:
            rounds.append(cur)
        return rounds

    @staticmethod
    def _format_rounds(rounds: list[list[Any]]) -> str:
        lines: list[str] = []
        for rnd in rounds:
            for item in rnd:
                role = AlingMemoryPlugin._role_of(item)
                text = AlingMemoryPlugin._text_of(item).strip()
                if not text:
                    continue
                who = "用户" if role == "user" else "阿凌"
                lines.append(f"{who}: {text}")
        return "\n".join(lines)

    @staticmethod
    def _keywords(text: str) -> set[str]:
        """粗糙但够用的关键词：中文按连续 2~4 字片段 + 英文/数字 token。"""
        text = re.sub(r"<system_reminder>.*?</system_reminder>", " ", text, flags=re.S)
        words: set[str] = set()
        for tok in re.findall(r"[A-Za-z0-9_]{2,}", text):
            words.add(tok.lower())
        zh = re.findall(r"[一-龥]+", text)
        for seg in zh:
            if len(seg) <= 4:
                words.add(seg)
            else:
                for i in range(0, len(seg) - 1):
                    words.add(seg[i : i + 2])
        return words

    def _identity_line(self, event: AstrMessageEvent) -> str:
        qq = event.get_sender_id() or ""
        name = event.get_sender_name() or ""
        gid = event.get_group_id() or ""
        if gid:
            kind = "群聊"
            place = f"群号 {gid}"
        else:
            kind = "私聊"
            place = "私聊窗口"
        umo = event.unified_msg_origin
        return (
            f"身份锚点(unified_id): {umo} | platform_id={qq} | 显示名={name or 'N/A'}"
            f" | {kind} {place}；本会话里「{qq}」是唯一稳定身份，昵称可能变化或重名"
        )

    # --------------------------------------------------------- 记账层（存档）
    @staticmethod
    def _comp_type(comp: Any) -> str:
        t = getattr(comp, "type", "")
        return str(getattr(t, "value", t) or "").lower()

    @classmethod
    def _plain_text(cls, event: AstrMessageEvent) -> str:
        """把消息里的纯文本拼起来；@ 段换成「@某人」，其它非文本段用占位符。"""
        parts: list[str] = []
        try:
            chain = event.get_messages() or []
        except Exception:  # noqa: BLE001
            chain = []
        for comp in chain:
            name = cls._comp_type(comp)
            if name == "plain":
                parts.append(str(getattr(comp, "text", "")))
            elif name == "at":
                qq = str(getattr(comp, "qq", ""))
                nm = str(getattr(comp, "name", "") or "")
                if qq == str(event.get_self_id()):
                    parts.append("@阿凌")
                else:
                    parts.append(f"@{nm or qq}")
            elif name == "reply":
                parts.append("〔回复〕")
            elif name in ("image", "record", "video", "file"):
                parts.append(
                    f"〔{ {'image': '图片', 'record': '语音', 'video': '视频', 'file': '文件'}[name] }〕"
                )
        text = "".join(parts).strip()
        if not text:
            text = (getattr(event, "message_str", "") or "").strip()
        return text

    def _group_meta(self, event: AstrMessageEvent) -> tuple[str, str, str]:
        gid = str(event.get_group_id() or "")
        gname = ""
        try:
            g = event.get_group()
            gname = str(getattr(g, "group_name", "") or "") if g else ""
        except Exception:  # noqa: BLE001
            gname = ""
        nick = event.get_sender_name() or ""
        card = ""
        try:
            raw = getattr(event.message_obj, "raw_message", None)
            sender = getattr(raw, "sender", None)
            if isinstance(sender, dict):
                card = str(sender.get("card") or "")
            elif sender is not None:
                card = str(getattr(sender, "card", "") or "")
        except Exception:  # noqa: BLE001
            card = ""
        return gid, gname, (card or nick)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=10)
    async def on_group_message(self, event: AstrMessageEvent):
        """群聊每条消息都落库（含没 @ 阿凌的）。只记不打扰：不回复、不调模型。"""
        if not self.cfg.get("group_archive_enable", True):
            return
        try:
            text = self._plain_text(event)
            if not text:
                return
            umo = event.unified_msg_origin
            gid, gname, card = self._group_meta(event)
            mid = ""
            try:
                mid = str(getattr(event.message_obj, "message_id", "") or "")
            except Exception:  # noqa: BLE001
                mid = ""
            ts = 0.0
            try:
                ts = float(getattr(event.message_obj, "timestamp", 0) or 0)
            except (TypeError, ValueError):
                ts = 0.0
            if ts <= 0:
                ts = time.time()
            at_bot = False
            try:
                self_id = str(event.get_self_id())
                at_bot = any(
                    str(getattr(c, "qq", "")) == self_id
                    for c in (event.get_messages() or [])
                    if self._comp_type(c) == "at"
                )
            except Exception:  # noqa: BLE001
                at_bot = False
            rid = self.store.add_group_message(
                umo=umo,
                group_id=gid,
                group_name=gname,
                message_id=mid,
                ts=ts,
                qq_id=str(event.get_sender_id() or ""),
                nickname=event.get_sender_name() or "",
                card=card,
                role="user",
                text=text,
                at_bot=at_bot,
                max_rows=int(self.cfg.get("group_archive_max_rows", 20000) or 0),
            )
            if rid:
                self.log(
                    f"archive umo={umo} gid={gid} qq={event.get_sender_id()} "
                    f"at={int(at_bot)} id={rid} text={text[:60]!r}"
                )
        except Exception as e:  # noqa: BLE001  记账失败绝不能影响正常聊天
            logger.warning(f"[aling-memory] 群消息存档失败：{e}")

    def _request_wants_identity(self) -> bool:
        try:
            cfg = self.context.get_config()
            return bool(cfg.get("provider_settings", {}).get("identifier", False))
        except Exception:  # noqa: BLE001
            return True

    # ------------------------------------------------------- 身份 + 记忆注入
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        umo = event.unified_msg_origin
        self._last_umo = umo
        qq = event.get_sender_id() or ""
        name = event.get_sender_name() or ""

        # 1) 身份锚点：跟 AstrBot 自带的 system_reminder 合成一块，避免刷屏
        if self.cfg.get("enable_identity", True) and self._request_wants_identity():
            line = self._identity_line(event)
            attached = False
            for part in reversed(req.extra_user_content_parts):
                text = getattr(part, "text", "")
                if isinstance(text, str) and text.rstrip().endswith("</system_reminder>"):
                    idx = text.rindex("</system_reminder>")
                    part.text = text[:idx] + "\n" + line + "\n" + text[idx:]
                    attached = True
                    break
            if not attached:
                req.extra_user_content_parts.append(
                    TextPart(text=f"{IDENTITY_START}\n{line}\n</system_reminder>")
                )
            self.log(f"identity umo={umo} qq={qq} name={name}")

        # 2) 短期：整轮裁剪
        max_turns = int(self.cfg.get("short_term_max_turns", 12) or 0)
        dropped_rounds: list[list[Any]] = []
        head, dialogue = self._split_history(list(req.contexts or []))
        rounds = self._group_into_rounds(dialogue)
        if max_turns > 0 and len(rounds) > max_turns:
            keep = max(1, max_turns - 1)
            cut = len(rounds) - keep
            dropped_rounds = rounds[:cut]
            kept_from_db = rounds[cut:]
            req.contexts = head + [m for rnd in kept_from_db for m in rnd]
            self.log(
                f"short-term umo={umo} rounds={len(rounds)} capped_to={keep} "
                f"dropped_rounds={cut} contexts_before={len(dialogue)} "
                f"contexts_after={len(req.contexts) - len(head)}"
            )
        else:
            self.log(
                f"short-term umo={umo} rounds={len(rounds)}/{max_turns} "
                f"contexts={len(dialogue)} (未封顶)"
            )

        if not self.cfg.get("mid_term_enable", True):
            return

        # 3) 中期：把被裁掉的轮 + 空闲过久的旧轮压成摘要
        prev_summary, covered = self.store.get_summary(umo)
        summarize_targets: list[list[Any]] = list(dropped_rounds)

        if not summarize_targets and covered == 0:
            # 首次运行且没有摘要：如果上下文很长，先压掉一部分做冷启动
            keep_turns = max(1, int(self.cfg.get("mid_term_keep_turns", 6)))
            if len(rounds) > keep_turns * 2:
                summarize_targets = rounds[:-keep_turns]
                req.contexts = head + [
                    m for rnd in rounds[-keep_turns:] for m in rnd
                ]
        elif not summarize_targets and covered > 0:
            # 已经有过摘要：把早于 covered 轮之外的旧轮压掉，保持封顶后仍有摘要
            keep_turns = max(1, int(self.cfg.get("mid_term_keep_turns", 6)))
            if len(rounds) > keep_turns:
                summarize_targets = rounds[:-keep_turns]
                req.contexts = head + [
                    m for rnd in rounds[-keep_turns:] for m in rnd
                ]

        if summarize_targets:
            body = self._format_rounds(summarize_targets)
            if len(body) > 40 and umo not in self._inflight:
                self._inflight.add(umo)
                try:
                    prov = await self.context.get_using_provider_async(umo)
                    if prov is not None:
                        prompt = SUMMARY_PROMPT.format(
                            prev=prev_summary or "（还没有）", body=body[:8000]
                        )
                        resp = await prov.text_chat(prompt=prompt, request_max_retries=1)
                        summary = (resp.completion_text or "").strip()
                        no_content = summary in {"（无重要内容）", "无", "（无）", ""}
                        if summary and not no_content:
                            new_covered = covered + len(summarize_targets)
                            self.store.set_summary(umo, summary, new_covered)
                            prev_summary = summary
                            self.log(
                                f"mid-term umo={umo} summarized_rounds={len(summarize_targets)} "
                                f"covered_turns={new_covered} summary_len={len(summary)}"
                            )
                        elif no_content:
                            new_covered = covered + len(summarize_targets)
                            self.store.set_summary(umo, prev_summary, new_covered)
                            self.log(
                                f"mid-term-keep umo={umo} rounds={len(summarize_targets)} "
                                f"no_new_content covered_turns={new_covered}"
                            )
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[aling-memory] 摘要生成失败：{e}")
                finally:
                    self._inflight.discard(umo)

        head, _ = self._split_history(list(req.contexts or []))
        intro: list[dict[str, Any]] = []
        if prev_summary:
            intro.append({"role": "user", "content": f"{SUMMARY_HEADER}\n{prev_summary}"})
            intro.append({"role": "assistant", "content": "（已记住这些早期对话要点。）"})
            self.log(f"inject-summary umo={umo} len={len(prev_summary)}")

        # 4) 长期：按 QQ 号检索注入
        if self.cfg.get("long_term_enable", True) and qq:
            scope = self.cfg.get("long_term_scope", "owner_only")
            # 按 QQ 号归档：跨群、跨私聊都能想起来（只属于这个人本人）
            facts = self.store.list_facts(qq)
            if scope == "shared_in_group" and event.get_group_id():
                facts = facts + [
                    f for f in self._all_group_facts(umo) if str(f["qq_id"]) != qq
                ]
            if facts:
                query_text = (req.prompt or "") + " " + (
                    event.message_str if hasattr(event, "message_str") else ""
                )
                qkw = self._keywords(query_text)
                ranked = sorted(
                    facts,
                    key=lambda f: (
                        len(qkw & self._keywords(str(f["content"]))),
                        float(f["updated_at"]),
                    ),
                    reverse=True,
                )[: int(self.cfg.get("long_term_top_k", 6))]
                body = "\n".join(
                    f"- [{f['qq_id']}{'/' + f['display'] if f['display'] else ''}] {f['content']}"
                    for f in ranked
                )
                intro.append(
                    {
                        "role": "user",
                        "content": FACT_HEADER.format(qq=qq)
                        + "\n（按 QQ 号归档，属于当前说话人本人；只用于理解他，不要当众复述）\n"
                        + body,
                    }
                )
                intro.append(
                    {"role": "assistant", "content": "（已读取该用户的长期记忆条目。）"}
                )
                self.log(
                    f"inject-facts umo={umo} qq={qq} total={len(facts)} "
                    f"picked={len(ranked)} scope={scope}"
                )

        # 5) 回看：只在问到过去的事时才查存档，日常请求不查库
        recall_block = self._recall_block(event, req)
        if recall_block:
            intro.append({"role": "user", "content": recall_block})
            intro.append({"role": "assistant", "content": "（已翻到这几位刚才说的话。）"})

        if intro:
            req.contexts = head + intro + (
                req.contexts[len(head) :] if head else list(req.contexts)
            )

    # --------------------------------------------------------- 回看层（检索）
    @staticmethod
    def _recall_query(text: str) -> str:
        """把「刚才谁说的串串」这类提问洗成检索关键词。"""
        q = re.sub(r"<system_reminder>.*?</system_reminder>", " ", text or "", flags=re.S)
        q = re.sub(r"@\S+", " ", q)
        # 去掉纯提问词，留下「实体词」用来匹配
        q = re.sub(
            r"(刚才|刚刚|之前|以前|上次|上回|昨天|前天|最近|谁说的|谁说了|谁说|哪个人|哪个|"
            r"聊了什么|聊了啥|说了什么|说了啥|讲了什么|讲了啥|发生了什么|怎么回事|什么情况|"
            r"记得|还记得|想不起来|不记得|历史|记录|回看|查一下|帮我查|找一下|搜一下|"
            r"群里|大家|清楚|主要|具体|谁|"
            r"吗|呢|啊|呀|吧|了|的|是|在|说|话|有|没|和|跟|个|吧)",
            " ",
            q,
        )
        return " ".join(q.split())[:80]

    def _recall_block(self, event: AstrMessageEvent, req: ProviderRequest) -> str:
        if not self.cfg.get("recall_enable", True):
            return ""
        if not event.get_group_id():
            return ""
        ask = (getattr(event, "message_str", "") or "").strip()
        if not ask:
            return ""
        if not RECALL_TRIGGER_RE.search(ask):
            self.log(f"recall-skip umo={event.unified_msg_origin} reason=no-trigger")
            return ""
        umo = event.unified_msg_origin
        top_k = max(1, int(self.cfg.get("recall_top_k", 8)))
        scan = max(top_k, int(self.cfg.get("recall_scan", 2000)))
        keyword = self._recall_query(ask)
        # 问「刚才/群里都聊了啥」这类泛问：光按关键词命中的几条会把别的人漏掉，
        # 所以再取一段最近的原文窗口，跟命中结果合并，保证人人都看得到。
        broad = not keyword or bool(RECALL_BROAD_RE.search(ask))
        try:
            hits: list[Any] = []
            if keyword:
                hits = list(
                    self.store.search_group_messages(
                        umo, keyword, limit=top_k, scan=scan
                    )
                )
            if broad:
                merged: dict[Any, Any] = {int(r["id"]): r for r in hits}
                for r in self.store.recent_group_messages(umo, limit=max(top_k, 10)):
                    merged.setdefault(int(r["id"]), r)
                rows = sorted(
                    merged.values(), key=lambda r: (float(r["ts"]), int(r["id"]))
                )[-max(top_k, 12):]
            elif hits:
                rows = hits
            else:
                # 有具体关键词但没命中：退回最近原文，别让模型凭空猜
                rows = self.store.recent_group_messages(umo, limit=max(top_k, 8))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[aling-memory] 历史检索失败：{e}")
            return ""
        if not rows:
            self.log(f"recall umo={umo} keyword={keyword!r} hits=0")
            return ""
        keyword = f"{keyword} |broad" if broad else keyword
        lines: list[str] = []
        prev_day = ""
        for r in rows:
            t = time.strftime("%m-%d %H:%M", time.localtime(float(r["ts"])))
            day = t[:5]
            if day != prev_day:
                lines.append(f"— {day} ——")
                prev_day = day
            who = str(r["card"] or r["nickname"] or r["qq_id"] or "某人").strip()
            text = str(r["text"]).replace("\n", " ")[:120]
            lines.append(f"[{t}] {who}: {text}")
        body = "\n".join(lines)
        self.log(
            f"recall umo={umo} keyword={keyword!r} hits={len(rows)} chars={len(body)}"
        )
        return (
            f"{ARCHIVE_HEADER}\n"
            "这是阿凌自己翻本地存档查到的原文，只有当前这一轮可见。\n"
            "回答要求：\n"
            "1. 只依据上面的记录回答，记录里没有的就直说没看到，别编；\n"
            "2. 提到人只用昵称/群名片（上面已经给的是显示名），绝对不要念任何数字 ID；\n"
            "3. 复述别人的话时，说清楚是谁说的、大概什么时候；\n"
            "4. 不要整段照抄记录，用自己的话转述 2~4 句就够。\n"
            f"{body}"
        )

    def _all_group_facts(self, umo: str) -> list[Any]:
        try:
            conn = self.store._conn  # noqa: SLF001  同包内直接复用连接
            return list(
                conn.execute(
                    "SELECT * FROM user_facts WHERE umo=? ORDER BY updated_at DESC LIMIT 50",
                    (umo,),
                ).fetchall()
            )
        except Exception:  # noqa: BLE001
            return []

    # -------------------------------------------------------- 长期事实抽取
    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        # 0) 出口兜底：任何 5~14 位连续数字都当作 QQ 号念出来了，直接删掉
        if self.cfg.get("reply_strip_qq", True):
            self._strip_qq_from_reply(resp, event)

        if not self.cfg.get("long_term_enable", True):
            return
        umo = event.unified_msg_origin
        qq = event.get_sender_id() or ""
        if not qq:
            return
        turns = self.store.bump_turn(umo)
        every = max(1, int(self.cfg.get("long_term_extract_every_turns", 2)))
        if turns % every != 0:
            return
        user_text = getattr(event, "message_str", "") or ""
        bot_text = (resp.completion_text or "").strip()
        # 只喂「本轮这名用户自己说的话」：阿凌的回复、历史里别人的话都可能被误挂到他头上
        body = f"用户({qq}，{event.get_sender_name() or '未知'}): {user_text}"
        if len(body) < 20:
            return
        try:
            prov = await self.context.get_using_provider_async(umo)
            if prov is None:
                return
            r = await prov.text_chat(
                prompt=FACT_PROMPT.format(qq=qq, body=body[:4000]),
                system_prompt="",
                request_max_retries=1,
            )
            out = (r.completion_text or "").strip()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[aling-memory] 事实抽取失败：{e}")
            return
        self.log(f"fact-raw umo={umo} qq={qq} out={out[:160]!r}")
        if not out or out.strip() in {"无", "（无）"}:
            return
        saved = 0
        for line in out.splitlines():
            line = line.strip()
            line = re.sub(r"^[-*•\d.、\s]+", "", line)
            if len(line) < 4 or line.startswith("无"):
                continue
            if self.store.add_fact(umo, qq, event.get_sender_name() or "", line):
                saved += 1
        if saved:
            self.log(f"fact umo={umo} qq={qq} saved={saved}")

    @filter.on_llm_response()
    async def archive_bot_reply(self, event: AstrMessageEvent, resp: LLMResponse):
        """把阿凌自己说过的话也存进群存档，回看时能对上「我问了什么、它答了什么」。"""
        if not self.cfg.get("group_archive_enable", True):
            return
        if not self.cfg.get("group_archive_store_bot", True):
            return
        if not event.get_group_id():
            return
        text = (resp.completion_text or "").strip()
        if not text:
            return
        try:
            gid, gname, _ = self._group_meta(event)
            mid = ""
            try:
                mid = str(getattr(event.message_obj, "message_id", "") or "")
            except Exception:  # noqa: BLE001
                mid = ""
            self.store.add_group_message(
                umo=event.unified_msg_origin,
                group_id=gid,
                group_name=gname,
                message_id=(mid + ":bot") if mid else "",
                ts=time.time(),
                qq_id=str(event.get_self_id() or "bot"),
                nickname="阿凌",
                card="阿凌",
                role="bot",
                text=text.replace("\n", " ")[:600],
                at_bot=False,
                max_rows=int(self.cfg.get("group_archive_max_rows", 20000) or 0),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[aling-memory] 机器人回复存档失败：{e}")

    def _strip_qq_from_reply(self, resp: LLMResponse, event: AstrMessageEvent) -> None:
        text = resp.completion_text or ""
        if not text:
            return

        def _repl(m: re.Match[str]) -> str:
            return " "

        fixed = _QQ_RUN_RE.sub(_repl, text)
        if fixed == text:
            return
        # 删除后可能留下「。 」这类空隙，统一收一下标点周围的空格
        fixed = re.sub(r"[ \t]+([，。！？；：、,.!?;:）)\]】」』])", r"\1", fixed)
        fixed = re.sub(r"([（(\[【「『])[ \t]+", r"\1", fixed)
        # 删完可能只剩一对空括号，一并收掉；行首空格也收掉
        fixed = re.sub(r"[（(\[【「『][ \t]*[）)\]】」』]", "", fixed)
        fixed = re.sub(r"^[ \t]+", "", fixed, flags=re.M)
        fixed = re.sub(r"[ \t]{2,}", " ", fixed)
        resp.completion_text = fixed
        hit = _QQ_RUN_RE.findall(text)
        umo = getattr(event, "unified_msg_origin", "-")
        self.log(f"reply-strip-qq umo={umo} removed={hit}")

    # ------------------------------------------------------------- 调试指令
    @filter.command("mem")
    async def mem(self, event: AstrMessageEvent):
        """记忆插件调试：/mem status | facts [QQ号] | summary | fact <文本> | forget [QQ号]"""
        args = (getattr(event, "message_str", "") or "").split()
        sub = args[1] if len(args) > 1 else "status"
        umo = event.unified_msg_origin
        qq = event.get_sender_id() or ""
        is_admin = bool(getattr(event, "is_admin", lambda: False)())
        lines: list[str] = []

        # 越权保护：普通成员只能看/改自己那份档案。
        # 指定别人 QQ 号（/mem facts <QQ>、/mem forget <QQ>）只允许管理员操作，
        # 否则任何人一条 /mem forget 就能删掉别人的长期记忆。
        # 不带参数的 /mem forget、/mem fact 仍然操作自己，不受限。
        target_arg = args[2] if len(args) > 2 else None
        touches_global = sub == "status"
        touches_other = (
            sub in {"facts", "forget"}
            and target_arg is not None
            and str(target_arg) != str(qq)
        )
        if (touches_global or touches_other) and not is_admin:
            self.log(f"mem-denied umo={umo} qq={qq} sub={sub} arg={target_arg}")
            if touches_global:
                yield event.plain_result(
                    "记忆总览只有管理员能看。你自己那份可以用 /mem facts 查看。"
                )
            else:
                yield event.plain_result(
                    "只能查/删你自己的记忆条目。要看别人的或清空全部，请找管理员。"
                )
            return

        if sub == "status":
            summary, covered = self.store.get_summary(umo)
            lines.append(f"会话：{umo}")
            lines.append(f"长期条目总数：{self.store.count_facts()}")
            lines.append(f"本会话摘要：{'有' if summary else '无'}（覆盖 {covered} 轮）")
            lines.append(f"当前 QQ 的长期条目：{self.store.count_facts(qq)} 条")
            lines.append(
                f"群存档：本会话 {self.store.count_group_messages(umo)} 条，"
                f"全库 {self.store.count_group_messages()} 条"
            )
            lines.append(
                f"配置：max_turns={self.cfg['short_term_max_turns']} "
                f"mid={self.cfg['mid_term_enable']} long={self.cfg['long_term_enable']} "
                f"scope={self.cfg['long_term_scope']} "
                f"archive={self.cfg.get('group_archive_enable', True)} "
                f"recall={self.cfg.get('recall_enable', True)}"
            )
        elif sub == "facts":
            target = args[2] if len(args) > 2 else qq
            facts = self.store.list_facts(target)
            lines.append(f"{target} 的长期条目（{len(facts)} 条）：")
            lines += [f"#{f['id']} {f['content']}" for f in facts[:20]]
        elif sub == "summary":
            summary, covered = self.store.get_summary(umo)
            lines.append(summary or "（本会话还没有摘要）")
            lines.append(f"— 覆盖 {covered} 轮")
        elif sub == "fact" and len(args) > 2:
            text = " ".join(args[2:])
            fid = self.store.add_fact(umo, qq, event.get_sender_name() or "", text)
            lines.append(f"已记下 #{fid}：{text}")
        elif sub == "forget":
            target = args[2] if len(args) > 2 else qq
            n = self.store.clear_user(target)
            lines.append(f"已删除 {target} 的 {n} 条长期条目")
        elif sub == "log" and len(args) > 2:
            # /mem log 串串 —— 管理员手动回看：按关键词翻群存档
            kw = " ".join(args[2:])
            rows = self.store.search_group_messages(umo, kw, limit=10)
            lines.append(f"「{kw}」相关记录（{len(rows)} 条）：")
            for r in rows:
                t = time.strftime("%m-%d %H:%M", time.localtime(float(r["ts"])))
                who = str(r["card"] or r["nickname"] or r["qq_id"])
                lines.append(f"[{t}] {who}: {str(r['text'])[:80]}")
        elif sub == "recent":
            rows = self.store.recent_group_messages(umo, limit=10)
            lines.append(f"本群最近存档（{len(rows)} 条）：")
            for r in rows:
                t = time.strftime("%m-%d %H:%M", time.localtime(float(r["ts"])))
                who = str(r["card"] or r["nickname"] or r["qq_id"])
                lines.append(f"[{t}] {who}: {str(r['text'])[:80]}")
        else:
            lines.append(
                "用法：/mem status | facts [QQ号] | summary | fact <文本> | forget [QQ号] "
                "| recent | log <关键词>"
            )
        yield event.plain_result("\n".join(lines))
