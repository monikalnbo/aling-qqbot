"""阿凌 · 语音回复插件（AstrBot 4.29.x，增量插件）

做什么
------
把阿凌生成好的**文字回复**，在发出去之前换成 QQ 语音消息：

    DeepSeek 回复文本 → 按 AstrBot 自己的分段规则拆句
                     → 每段各自合成（SpeechSynthesizer，直接出 mp3）
                     → 一段一条 QQ record，按原顺序连续发出

默认 **开启**（重启后保持开启，状态写在插件自己的 state.json）。管理员仍可切换：

    语音模式   → 之后的回复发语音
    文字模式   → 之后的回复恢复成文字
    语音状态   → 看现在是哪种模式

分条规则
--------
* 拆句**复用 AstrBot 的分段回复配置**（`platform_settings.segmented_reply`，
  现在用的是正则 `(?s).*?\\n{2,}|.*?[。？！]+|.+$`），不另造一套规则。
* 拆出来的每一段各合成一条语音；条与条之间的间隔沿用 AstrBot 分段回复的
  随机间隔（现在 0.5~1.2s），由发送阶段统一控制。
* 动作描写（`*揉眼睛*`、`（叹气）`）**不念**：整段只有动作就跳过不发；
  动作和台词在同一段时，剔掉动作只念台词。理由：TTS 没有表演能力，
  把"揉着眼睛往被子里缩"念出来反而很怪。

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
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain, Record
from astrbot.api.star import Context, Star, StarTools, register

PLUGIN_NAME = "astrbot_plugin_aling_voice"

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
}

# 非实时语音合成的固定路径（百炼 SpeechSynthesizer）
TTS_PATH = "/api/v1/services/audio/tts/SpeechSynthesizer"

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


@register(
    PLUGIN_NAME,
    "aling",
    "文字回复转 QQ 语音消息（默认开启，按分段规则一条一段，百炼非实时语音合成）",
    "1.2.1",
)
class AlingVoicePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        cfg = dict(DEFAULT_CONFIG)
        if isinstance(config, dict):
            cfg.update({k: v for k, v in config.items() if v is not None})
        self.cfg = cfg
        self.voice_on = bool(cfg.get("enable_on_start", True))
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = self.data_dir / "audio"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.data_dir / "state.json"
        self._load_state()

    # ------------------------------------------------------------ 状态
    def _load_state(self) -> None:
        try:
            with open(self.state_file, encoding="utf-8") as f:
                st = json.load(f)
            self.voice_on = bool(st.get("voice_on", self.voice_on))
        except Exception:  # noqa: BLE001
            pass

    def _save_state(self) -> None:
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump({"voice_on": self.voice_on, "updated_at": time.strftime("%F %T")}, f)
        except Exception as exc:  # noqa: BLE001
            self.log(f"保存开关状态失败：{exc}")

    def log(self, msg: str) -> None:
        logger.info(f"[aling-voice] {msg}")

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_admin())
        except Exception:  # noqa: BLE001
            return False

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
        """/语音状态 —— 看看现在是语音还是文字"""
        event.set_extra("aling_voice_skip", True)
        mode = "语音" if self.voice_on else "文字"
        yield event.plain_result(
            f"现在是【{mode}】模式。管理员发「文字模式」/「语音模式」切换。"
        )

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
