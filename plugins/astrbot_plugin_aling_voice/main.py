"""阿凌 · 语音回复插件（AstrBot 4.29.x，增量插件）v1.3.0

做什么
------
把阿凌生成好的**文字回复**，在发出去之前换成 QQ 语音消息：

    DeepSeek 回复文本 → 按 AstrBot 自己的分段规则拆句
                     → 每段各自合成（SpeechSynthesizer，直接出 mp3）
                     → 一段一条 QQ record，按原顺序连续发出

唱歌模式（v1.3.0 新增）
----------------------
日常说话和唱歌用**两条独立链路**，互不影响：

* **日常聊天**：模型 `qwen-audio-3.0-tts-flash` + 龙安小昕，一句一条，快
  （实测单条约 0.8 秒出音）。
* **唱歌**：判断到点歌意图后，先让对话模型写歌词，再换
  `qwen-audio-3.1-tts-next` + `text_prompt`（唱法提示）合成。只有这个模型
  会真的唱出旋律（实测稳定长音 37/122 个，朗读版 0 个）。唱一首原句约
  60–70 秒、多行歌词约 45 秒，所以唱歌是「等一下，然后听」的节奏。

点歌意图怎么判断（规则都在 `sing.py`，宁可不唱、不要乱唱）：

* 会唱：`唱歌` `唱首歌` `唱首《两只老虎》` `来一首小星星` `我要听歌`
  `点歌 晴天` `唱歌模式` `给我唱一段` …
* 不会唱：`不要唱歌` `别唱了` `你会唱歌吗` `他唱得真好听` `谁在唱歌`
  `教我唱歌` `聊聊天吧` …

另外提供显式口令（绕开识别规则，想稳就用它）：

    /唱歌 两只老虎     → 唱指定歌
    /唱歌              → 让阿凌自己挑一首（儿歌/公共歌曲）
    /唱歌 关闭         → 彻底关掉唱歌（管理员）
    /唱歌 开启         → 重新打开（管理员）

分条规则
--------
* 拆句**复用 AstrBot 的分段回复配置**（`platform_settings.segmented_reply`，
  现在用的是正则 `(?s).*?\\n{2,}|.*?[。？！]+|.+$`），不另造一套规则。
* 拆出来的每一段各合成一条语音；条与条之间的间隔沿用 AstrBot 分段回复的
  随机间隔（现在 0.5~1.2s），由发送阶段统一控制。
* 动作描写（`*揉眼睛*`、`（叹气）`）**不念**：整段只有动作就跳过不发；
  动作和台词在同一段时，剔掉动作只念台词。理由：TTS 没有表演能力，
  把"揉着眼睛往被子里缩"念出来反而很怪。
* 唱歌同理：歌词最多拆 `sing_max_segments` 段、每段不超过
  `sing_max_chars` 字，避免一条 95 秒的语音在 QQ 端被截断。

设计取舍
--------
* 不改 AstrBot 的对话逻辑、人设、记忆插件：只在 LLM 结果发出去之前那一步替换。
* 不动 AstrBot 自带的 TTS Provider 配置（默认值是关闭的），状态存在插件自己的
  配置文件里（`data/config/astrbot_plugin_aling_voice_config.json`，权限 600）。
* 音色默认 `longanxiaoxin`（龙安小昕，22 岁亲切活泼音）：用户反馈 longanlingxi
  听着偏老，要元气甜美的少女音。想换回旧的改配置 `voice`，或用
  `python3 scripts/set_voice.py longanlingxi`。
* 合成失败、超时、超过字数上限：该段退回发文字，并把原因写进日志，不吞消息。
* 语音接口走百炼「非实时语音合成」的 SpeechSynthesizer 接口
  (`<主机>/api/v1/services/audio/tts/SpeechSynthesizer`)，一次 POST 拿到音频 URL，
  再下载 mp3；`volume` 参数 0~100 由服务端直接放大，不靠本地二次增益。
  走 chat/completions 的 omni 方案调不通这个模型，实测过程见 docs/实测记录.md。
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain, Record
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register

from .sing import detect_singing, prepare_lyrics

PLUGIN_NAME = "astrbot_plugin_aling_voice"
PLUGIN_VERSION = "1.3.0"

DEFAULT_CONFIG: dict[str, Any] = {
    "enable_on_start": True,
    "api_base": "https://maas.qianwenaiapi.com",
    "api_key": "",
    "model": "qwen-audio-3.0-tts-flash",
    "voice": "longanxiaoxin",
    "volume": 100,
    "sample_rate": 24000,
    "max_chars": 120,
    "max_concurrency": 3,
    "dual_output": False,
    "timeout": 60,
    # ---- 唱歌模式 ----
    "sing_enabled": True,
    "sing_auto": True,
    "sing_model": "qwen-audio-3.1-tts-next",
    "sing_voice": "",
    "sing_prompt": "请用唱歌的方式，带着旋律把这段歌词唱出来，像在唱儿歌一样，每个字拖长、有起伏地演唱",
    "sing_max_chars": 40,
    "sing_max_segments": 2,
    "sing_timeout": 150,
    "sing_extra_words": [],
    "sing_extra_regex": "",
}

# 非实时语音合成的固定路径（百炼 SpeechSynthesizer）
TTS_PATH = "/api/v1/services/audio/tts/SpeechSynthesizer"

# 点歌时追加给对话模型的写作要求（临时，不写进人设、不进历史）
SING_LYRIC_PROMPT = """【本次需要唱歌 —— 只输出歌词，不要任何解释】
用户想听你唱歌。请直接输出要唱的歌词，最多 {n} 行、每行不超过 {m} 个字：
- 优先用用户点的那首歌（只写副歌或最经典的几句，不要整首几十行）；
- 用户没指定歌名时，从儿歌或公有领域的老歌里挑一首（小星星、两只老虎、生日快乐、
  茉莉花、送别、让我们荡起双桨等）；
- 只写歌词本身：不要写歌名、不要加「（唱）」「*动作*」、不要标点堆叠、不要解释；
- 不要出现你自己的口癖和称呼，用户要听的是一首歌。"""

# AstrBot 自带分段回复的兜底规则（读不到配置时用，和 AstrBot 默认值一致）
_DEFAULT_SPLIT_REGEX = r"(?s).*?\n{2,}|.*?[。？！]+|.+$"
_DEFAULT_SPLIT_WORDS = ["。", "？", "！", "~", "…"]

# 语音里不该念出来的痕迹：markdown、动作/表情描写（*...*、（...））、空白
_MD_RE = re.compile(r"[*_`#>]+")
_PAREN_RE = re.compile(r"[（(][^）)]{0,30}[）)]")
_ACTION_RE = re.compile(r"[*＊][^*＊]{0,60}[*＊]")
_SPACE_RE = re.compile(r"\s+")
_PUNCT_SPACE_RE = re.compile(r"\s*([，。；：？！、])\s*")
_DUP_PUNCT_RE = re.compile(r"([，。；：？！、])\1+")
_EMOJI_RE = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff️‍]+"
)
# 超长句子再切时用的句末标点
_SENT_RE = re.compile(r"[^。！？!?；;…]+[。！？!?；;…]*")


_WAKE_PREFIXES = ("/", "／", "!", "！")


def _strip_cmd_prefix(text: str, command: str) -> str:
    """去掉消息开头的唤醒前缀（/、! 等）和指令名，剩下的就是参数。

    AstrBot 的 message_str 里在「群聊 @ 机器人」时仍带着唤醒前缀，
    直接用 message_str 会把「/唱歌」本身当成歌名。
    """
    t = (text or "").strip()
    for _ in range(2):
        for p in _WAKE_PREFIXES:
            if t.startswith(p):
                t = t[len(p) :].lstrip()
                break
        else:
            break
    for name in (command, command.lstrip("/")):
        if t.startswith(name):
            t = t[len(name) :].lstrip("：:，, ").strip()
            break
    return t


@register(
    PLUGIN_NAME,
    "aling",
    "文字回复转 QQ 语音消息 + 唱歌模式（一句一条，百炼非实时语音合成）",
    PLUGIN_VERSION,
)
class AlingVoicePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        cfg = dict(DEFAULT_CONFIG)
        if isinstance(config, dict):
            cfg.update({k: v for k, v in config.items() if v is not None})
        self.cfg = cfg
        self.voice_on = bool(cfg.get("enable_on_start", True))
        self.sing_on = bool(cfg.get("sing_enabled", True))
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = self.data_dir / "audio"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.data_dir / "state.json"
        # 每个会话最近一次点歌请求：{chat_id: {"title": str, "at": float, "explicit": bool}}
        self._song_pending: dict[str, dict] = {}
        self._load_state()

    # ------------------------------------------------------------ 状态
    def _load_state(self) -> None:
        try:
            with open(self.state_file, encoding="utf-8") as f:
                st = json.load(f)
            self.voice_on = bool(st.get("voice_on", self.voice_on))
            self.sing_on = bool(st.get("sing_enabled", self.sing_on))
        except Exception:  # noqa: BLE001
            pass

    def _save_state(self) -> None:
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "voice_on": self.voice_on,
                        "sing_enabled": self.sing_on,
                        "updated_at": time.strftime("%F %T"),
                    },
                    f,
                )
        except Exception as exc:  # noqa: BLE001
            self.log(f"保存开关状态失败：{exc}")

    def log(self, msg: str) -> None:
        logger.info(f"[aling-voice] {msg}")

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_admin())
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _chat_id(event: AstrMessageEvent) -> str:
        return str(getattr(event, "unified_msg_origin", "") or "")

    # ------------------------------------------------------------ 开关口令
    @filter.command("语音模式")
    async def cmd_voice_on(self, event: AstrMessageEvent):
        """/语音模式 —— 之后的回复改为发语音（管理员）"""
        if not self._is_admin(event):
            yield event.plain_result("这个开关只有管理员能碰，杂鱼就别乱按了。")
            return
        self.voice_on = True
        self._save_state()
        event.set_extra("aling_voice_skip", True)
        self.log(f"voice-on by={event.get_sender_id()} umo={event.unified_msg_origin}")
        yield event.plain_result("语音模式开了，接下来我用嘴说——别嫌吵。")

    @filter.command("文字模式")
    async def cmd_voice_off(self, event: AstrMessageEvent):
        """/文字模式 —— 回复恢复成文字（管理员）"""
        if not self._is_admin(event):
            yield event.plain_result("这个开关只有管理员能碰。")
            return
        self.voice_on = False
        self._save_state()
        event.set_extra("aling_voice_skip", True)
        self.log(f"voice-off by={event.get_sender_id()} umo={event.unified_msg_origin}")
        yield event.plain_result("切回文字了，省得你听不清。")

    @filter.command("语音状态")
    async def cmd_voice_state(self, event: AstrMessageEvent):
        """/语音状态 —— 看看现在是语音还是文字、唱歌开没开"""
        event.set_extra("aling_voice_skip", True)
        mode = "语音" if self.voice_on else "文字"
        sing = "开" if self.sing_on else "关"
        yield event.plain_result(
            f"现在是【{mode}】模式，唱歌模式【{sing}】。\n"
            f"管理员可发「文字模式」/「语音模式」切换说话方式；"
            f"唱歌直接说「唱首歌」或「来一首小星星」，也可以发「/唱歌 歌名」。"
        )

    # ------------------------------------------------------------ 唱歌口令
    @filter.command("唱歌")
    async def cmd_sing(self, event: AstrMessageEvent):
        """/唱歌 [歌名] —— 显式点歌；不带歌名由阿凌自己挑"""
        event.set_extra("aling_voice_skip", True)
        arg = _strip_cmd_prefix(event.message_str, "唱歌")
        if arg in {"关闭", "关", "off", "0"}:
            if not self._is_admin(event):
                yield event.plain_result("唱歌开关只有管理员能关。")
                return
            self.sing_on = False
            self._save_state()
            yield event.plain_result("唱歌关掉了，以后只说话不唱。")
            return
        if arg in {"开启", "开", "on", "1"}:
            if not self._is_admin(event):
                yield event.plain_result("唱歌开关只有管理员能开。")
                return
            self.sing_on = True
            self._save_state()
            yield event.plain_result("唱歌开回来了，想听就点。")
            return
        if not self.sing_on:
            yield event.plain_result("唱歌现在被我关着，管理员说「/唱歌 开启」才唱。")
            return
        if not self.voice_on:
            yield event.plain_result("现在是文字模式，先发「语音模式」我才能唱给你听。")
            return
        title = arg[:20] if arg else ""
        self.log(f"sing-request explicit title={title!r} umo={event.unified_msg_origin}")
        yield event.plain_result(
            f"行，我去准备{'《' + title + '》' if title else '一首歌'}，唱完可能得等一会儿。"
        )
        lyrics = await self._llm_lyrics(event, title)
        segments = prepare_lyrics(
            lyrics,
            int(self.cfg.get("sing_max_chars", 40)),
            int(self.cfg.get("sing_max_segments", 2)),
        )
        if not segments:
            yield event.plain_result("脑子里一时没翻出合适的歌词，你点一首具体的试试？")
            return
        made = await self._synth_song_segments(segments)
        if not made:
            yield event.plain_result("这次没唱出来（合成接口没给音频），过会儿再点一次？")
            return
        self.log(
            f"sing-done via=command title={title!r} segments={len(made)}/{len(segments)} "
            f"umo={event.unified_msg_origin}"
        )
        yield event.chain_result(
            [Record(file=str(p), path=str(p), text=seg) for p, seg in made]
        )

    # ------------------------------------------------------------ 意图 → 歌词
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """聊天里提到想听歌时，临时要求模型这轮只输出歌词。"""
        if event.get_extra("aling_sing_internal"):
            return  # 自己发起的歌词生成，不再递归注入
        if event.get_platform_name() != "aiocqhttp":
            return
        if not self.sing_on or not self.cfg.get("sing_auto", True):
            return
        text = (event.message_str or "").strip()
        if not text:
            return
        hit, title = detect_singing(
            text,
            self.cfg.get("sing_extra_words") or [],
            str(self.cfg.get("sing_extra_regex") or ""),
        )
        if not hit:
            return
        self._song_pending[self._chat_id(event)] = {
            "title": title, "at": time.time(), "explicit": False,
        }
        prompt = SING_LYRIC_PROMPT.format(
            n=int(self.cfg.get("sing_max_segments", 2)),
            m=int(self.cfg.get("sing_max_chars", 40)),
        )
        req.system_prompt = ((req.system_prompt or "") + "\n\n" + prompt).strip()
        self.log(
            f"sing-intent hit=True title={title!r} auto=True text={text!r} "
            f"umo={event.unified_msg_origin}"
        )

    async def _llm_lyrics(self, event: AstrMessageEvent, title: str) -> str:
        """让对话模型写一段歌词（只写歌词，不带人设口癖）。"""
        ask = (
            f"请直接输出《{title}》最适合唱的几句歌词（最多 {self.cfg.get('sing_max_segments', 2)} 行，"
            f"每行不超过 {self.cfg.get('sing_max_chars', 40)} 字），只输出歌词本身，不要歌名、不要解释。"
            if title
            else (
                "请从儿歌或公有领域老歌里挑一首，直接输出最适合唱的几句歌词"
                f"（最多 {self.cfg.get('sing_max_segments', 2)} 行，每行不超过 "
                f"{self.cfg.get('sing_max_chars', 40)} 字），只输出歌词本身，不要歌名、不要解释。"
            )
        )
        try:
            prov = self.context.get_using_provider()
            event.set_extra("aling_sing_internal", True)
            try:
                resp = await prov.text_chat(
                    prompt=ask,
                    contexts=[],
                    system_prompt="你是一个歌词转写器，只输出歌词正文，不寒暄、不加解释、不加动作描写。",
                    event=event,
                )
            finally:
                event.set_extra("aling_sing_internal", False)
            text = getattr(resp, "completion_text", "") or ""
            return text.strip()
        except Exception as exc:  # noqa: BLE001
            self.log(f"sing-lyrics LLM 失败：{exc}")
            return ""

    # ------------------------------------------------------------ 核心：文字→多条语音
    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """在消息发出前把文字按段换成多条语音（只对普通文字回复生效）。"""
        if not self.voice_on:
            return
        if event.get_extra("aling_voice_skip"):
            return  # 开关自身的确认语仍用文字回，方便一眼看到状态
        if event.get_platform_name() != "aiocqhttp":
            return
        result = event.get_result()
        if result is None or not getattr(result, "chain", None):
            return
        # 只处理「纯文字」结果：带图片/文件/表情等其他组件时不动
        if not all(isinstance(c, Plain) for c in result.chain):
            return
        original = "".join(c.text for c in result.chain).strip()
        if len(original) < 2:
            return

        # ---- 唱歌分支：这条回复前面有过点歌请求 → 用唱歌模型合成 ----
        pending = self._pop_song_pending(event)
        if pending is not None and self.sing_on:
            if await self._deliver_song(event, result, original, pending):
                return

        pieces = self.split_like_astrbot(original, self._seg_cfg(event))
        if not pieces:
            pieces = [original]

        # 先把每段清洗、切块，得到待合成任务；只含动作的段直接跳过
        jobs: list[tuple[str, str]] = []  # (该段原文, 要念的文本)
        skipped = 0
        for piece in pieces:
            speakable = self.clean_for_speech(piece)
            if len(speakable) < 2:
                skipped += 1
                self.log(f"voice-skip 纯动作/空段：{piece[:40]!r}")
                continue
            for chunk in self._chunk(speakable, int(self.cfg["max_chars"])):
                jobs.append((piece, chunk))
        if not jobs:
            self.log(f"voice-skip 整条都是动作，不发任何消息 umo={event.unified_msg_origin}")
            result.chain = []
            return

        # 并发合成（有上限），结果按原顺序落位
        sem = asyncio.Semaphore(max(1, int(self.cfg.get("max_concurrency", 3))))

        async def synth_one(text: str) -> Path | None:
            async with sem:
                try:
                    return await asyncio.wait_for(
                        self.synth_mp3(text), timeout=float(self.cfg["timeout"])
                    )
                except Exception as exc:  # noqa: BLE001
                    self.log(f"合成失败（该段退回文字）：{exc}")
                    return None

        audios = await asyncio.gather(*(synth_one(chunk) for _, chunk in jobs))

        new_chain: list[Any] = []
        kinds: list[str] = []
        for i, ((piece, chunk_text), mp3) in enumerate(zip(jobs, audios, strict=True), 1):
            if mp3:
                new_chain.append(Record(file=str(mp3), path=str(mp3), text=piece))
                kinds.append("voice")
                self.log(
                    f"voice-part {i}/{len(jobs)} voice file={mp3.name} "
                    f"chars={len(chunk_text)} head={chunk_text[:14]!r}"
                )
            else:
                new_chain.append(Plain(piece))
                kinds.append("text")
                self.log(f"voice-part {i}/{len(jobs)} text-fallback head={chunk_text[:14]!r}")
            if self.cfg.get("dual_output"):
                new_chain.append(Plain(piece))
        result.chain = new_chain
        self.log(
            f"voice-batch n={len(new_chain)} voice={kinds.count('voice')} "
            f"text={kinds.count('text')} skipped={skipped} "
            f"voice_id={self.cfg.get('voice')} volume={self.cfg.get('volume')} "
            f"umo={event.unified_msg_origin}"
        )

    # ------------------------------------------------------------ 唱歌：合成 + 发送
    def _pop_song_pending(self, event: AstrMessageEvent) -> dict | None:
        """取走本会话的点歌标记；超过 3 分钟视为早已作废。"""
        item = self._song_pending.pop(self._chat_id(event), None)
        if not item:
            return None
        if time.time() - float(item.get("at", 0)) > 180:
            self.log("sing-pending 已过期，本次按普通回复处理")
            return None
        return item

    async def _deliver_song(
        self, event: AstrMessageEvent, result: Any, original: str, pending: dict
    ) -> bool:
        """把这条回复换成歌声；失败返回 False，让调用方按普通语音处理。"""
        title = str(pending.get("title") or "")
        text = original
        segments = prepare_lyrics(
            text,
            int(self.cfg.get("sing_max_chars", 40)),
            int(self.cfg.get("sing_max_segments", 2)),
        )
        if not segments:
            self.log(f"sing-skip 没有可用歌词 title={title!r} umo={event.unified_msg_origin}")
            return False

        made = await self._synth_song_segments(segments)
        if not made:
            self.log(
                f"sing-fail 全部段落合成失败，退回普通语音链路 "
                f"title={title!r} umo={event.unified_msg_origin}"
            )
            return False  # 保留原文字结果，由后续流程按普通方式处理

        result.chain = [Record(file=str(p), path=str(p), text=seg) for p, seg in made]
        self.log(
            f"sing-done via=chat model={self.cfg.get('sing_model')} voice="
            f"{self.cfg.get('sing_voice') or self.cfg.get('voice')} "
            f"segments={len(made)}/{len(segments)} "
            f"dur={','.join(f'{self._audio_seconds(p):.1f}' for p, _ in made)}s "
            f"head={segments[0][:16]!r} umo={event.unified_msg_origin}"
        )
        return True

    async def _synth_song_segments(self, segments: list[str]) -> list[tuple[Path, str]]:
        """并发合成（最多 2 段同时），按原顺序返回成功的（文件, 歌词）。"""
        sem = asyncio.Semaphore(2)
        timeout = float(self.cfg.get("sing_timeout", 150))
        t0 = time.time()

        async def one(seg: str, idx: int) -> Path | None:
            async with sem:
                try:
                    return await asyncio.wait_for(self.synth_song(seg, idx), timeout=timeout)
                except Exception as exc:  # noqa: BLE001
                    self.log(f"sing 合成失败（第 {idx + 1} 段）：{exc}")
                    return None

        paths = await asyncio.gather(*(one(seg, i) for i, seg in enumerate(segments)))
        made = [(p, seg) for p, seg in zip(paths, segments) if p]
        self.log(
            f"sing-synth n={len(made)}/{len(segments)} elapsed={time.time() - t0:.1f}s"
        )
        return made

    async def synth_song(self, text: str, idx: int = 0) -> Path | None:
        key = (self.cfg.get("api_key") or "").strip()
        if not key:
            raise RuntimeError("未配置语音接口密钥（插件配置里的 api_key）")
        name = (
            "s_" + time.strftime("%Y%m%d_%H%M%S") + f"_{idx}_"
            + str(abs(hash(text)) % 100000)
        )
        data, ext = await asyncio.to_thread(self._request_sing_audio, text, key)
        if len(data) < 2000:
            raise RuntimeError(f"接口返回的音频太小（{len(data)} 字节）")
        out = self.cache_dir / f"{name}{ext}"
        with open(out, "wb") as f:
            f.write(data)
        return out if out.exists() and out.stat().st_size > 2000 else None

    def _request_sing_audio(self, text: str, key: str) -> tuple[bytes, str]:
        """用唱歌模型合成一段歌词。参数按实测可用的最小集来，别乱加。"""
        base = str(self.cfg["api_base"]).rstrip("/")
        if base.endswith("/api/v1"):
            base = base[: -len("/api/v1")]
        body = {
            "model": str(self.cfg.get("sing_model") or "qwen-audio-3.1-tts-next"),
            "input": {
                "text": text,
                "voice": str(self.cfg.get("sing_voice") or self.cfg.get("voice") or "longanxiaoxin"),
                "format": "mp3",
                "sample_rate": int(self.cfg.get("sample_rate", 24000)),
                "volume": int(self.cfg.get("volume", 100)),
                "text_prompt": str(self.cfg.get("sing_prompt") or DEFAULT_CONFIG["sing_prompt"]),
            },
        }
        timeout = float(self.cfg.get("sing_timeout", 150))
        req = urllib.request.Request(
            base + TTS_PATH,
            data=json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                obj = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:400].decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from None
        if obj.get("code") and obj.get("message"):
            raise RuntimeError(f"{obj['code']}: {obj['message']}")
        audio = ((obj.get("output") or {}).get("audio") or {})
        url = audio.get("url")
        if not url:
            raise RuntimeError("接口没返回音频地址")
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = r.read()
        ext = ".wav" if data[:4] == b"RIFF" else ".mp3"
        return data, ext

    @staticmethod
    def _audio_seconds(path: Path) -> float:
        """量一下音频有多长（容器里有 ffmpeg；没有就返回 0，不影响发送）。"""
        try:
            r = subprocess.run(
                ["ffmpeg", "-v", "quiet", "-i", str(path), "-ac", "1", "-ar", "16000",
                 "-f", "s16le", "-"],
                capture_output=True, timeout=60,
            )
            return len(r.stdout) / 2 / 16000
        except Exception:  # noqa: BLE001
            return 0.0

    # ------------------------------------------------------------ 复用 AstrBot 的分段规则
    def _seg_cfg(self, event: AstrMessageEvent) -> dict:
        try:
            cfg = self.context.get_config(event.unified_msg_origin)
            return dict(cfg.get("platform_settings", {}).get("segmented_reply", {}) or {})
        except Exception as exc:  # noqa: BLE001
            self.log(f"读取分段回复配置失败，用默认规则：{exc}")
            return {}

    @staticmethod
    def split_like_astrbot(text: str, seg_cfg: dict) -> list[str]:
        """按 AstrBot 分段回复的配置拆句，返回去掉空白的段落。"""
        try:
            threshold = int(seg_cfg.get("words_count_threshold", 500) or 500)
        except Exception:  # noqa: BLE001
            threshold = 500
        if len(text) > threshold:
            return [text]

        mode = seg_cfg.get("split_mode", "regex")
        if mode == "words":
            words = seg_cfg.get("split_words") or _DEFAULT_SPLIT_WORDS
            pattern = re.compile(
                "(.*?(" + "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True)) + ")|.+$)",
                re.DOTALL,
            )
            parts = [m[0] for m in pattern.findall(text)]
        else:
            pattern = seg_cfg.get("regex") or _DEFAULT_SPLIT_REGEX
            try:
                parts = re.findall(pattern, text, re.DOTALL | re.MULTILINE)
            except re.error:
                parts = re.findall(_DEFAULT_SPLIT_REGEX, text, re.DOTALL | re.MULTILINE)
        return [p.strip() for p in parts if p and p.strip()]

    @staticmethod
    def _chunk(text: str, limit: int) -> list[str]:
        """单段太长时按句末标点再切，保证一条语音别超过 limit 字。"""
        if limit <= 0 or len(text) <= limit:
            return [text]
        parts = _SENT_RE.findall(text) or [text]
        out: list[str] = []
        buf = ""
        for p in parts:
            while len(p) > limit:
                if buf:
                    out.append(buf)
                    buf = ""
                out.append(p[:limit])
                p = p[limit:]
            if len(buf) + len(p) <= limit:
                buf += p
            else:
                if buf:
                    out.append(buf)
                buf = p
        if buf:
            out.append(buf)
        return [c for c in out if c.strip()]

    # ------------------------------------------------------------ 文本清洗
    @staticmethod
    def clean_for_speech(text: str) -> str:
        """去掉动作描写、markdown、表情和多余符号，只留能念出来的话。"""
        t = _ACTION_RE.sub("", text)
        t = _PAREN_RE.sub("", t)
        t = _EMOJI_RE.sub("", t)
        t = _MD_RE.sub("", t)
        t = t.replace("|", " ，").replace("—", "，").replace("…", "。").replace("\n", " ")
        t = _SPACE_RE.sub(" ", t)
        # 标点两侧不留空格，连续标点只留一个
        t = _PUNCT_SPACE_RE.sub(r"\1", t)
        t = _DUP_PUNCT_RE.sub(r"\1", t)
        t = re.sub(r"[，、]\s*(?=[，。；：？！、])", "", t)
        return t.strip(" ，。；：、~")

    # ------------------------------------------------------------ 合成
    async def synth_mp3(self, text: str) -> Path | None:
        key = (self.cfg.get("api_key") or "").strip()
        if not key:
            raise RuntimeError("未配置语音接口密钥（插件配置里的 api_key）")
        name = "v_" + time.strftime("%Y%m%d_%H%M%S") + "_" + str(abs(hash(text)) % 100000)
        data, ext = await asyncio.to_thread(self._request_audio, text, key)
        if len(data) < 1000:
            raise RuntimeError(f"接口返回的音频太小（{len(data)} 字节）")
        out = self.cache_dir / f"{name}{ext}"
        with open(out, "wb") as f:
            f.write(data)
        return out if out.exists() and out.stat().st_size > 1000 else None

    def _request_audio(self, text: str, key: str) -> tuple[bytes, str]:
        """调百炼非实时语音合成，返回（音频字节, 后缀）。

        volume 是接口原生参数（0~100），由服务端放大，这里不做事后增益。
        实测这个服务即使要 mp3 也可能回 wav，按文件头定后缀，免得 QQ 那边拿去解码出错。
        """
        base = str(self.cfg["api_base"]).rstrip("/")
        # 允许 api_base 填开放平台域名或填 .../api/v1 形式，两种都能拼对
        if base.endswith("/api/v1"):
            base = base[: -len("/api/v1")]
        body = {
            "model": self.cfg["model"],
            "input": {
                "text": text,
                "voice": self.cfg["voice"],
                "format": "mp3",
                "sample_rate": int(self.cfg.get("sample_rate", 24000)),
                "volume": int(self.cfg.get("volume", 100)),
            },
        }
        timeout = float(self.cfg["timeout"])
        req = urllib.request.Request(
            base + TTS_PATH,
            data=json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                obj = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:400].decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from None
        if obj.get("code") and obj.get("message"):
            raise RuntimeError(f"{obj['code']}: {obj['message']}")
        audio = ((obj.get("output") or {}).get("audio") or {})
        url = audio.get("url")
        if not url:
            raise RuntimeError("接口没返回音频地址")
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = r.read()
        ext = ".wav" if data[:4] == b"RIFF" else ".mp3"
        return data, ext

    # ------------------------------------------------------------ 生命周期
    async def terminate(self) -> None:
        # 清掉 1 天前的合成缓存
        cutoff = time.time() - 86400
        for p in self.cache_dir.glob("*"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass
        self.log("已卸载")
