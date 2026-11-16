"""唱歌模式的意图识别与歌词规整（纯函数，方便单测）。

设计原则
--------
1. **宁可不唱，不要乱唱**：普通聊天、讨论「唱歌」这件事、否定句（不要唱/别唱）、
   问句（你会唱歌吗）、形容唱得好不好，都不触发。
2. 先说清「否定/元话题」规则，再看「点歌」规则；否定优先。
3. 歌名优先从《》里取，其次从「来一首小星星」「唱两只老虎」这类句式里取。

对外只暴露两个函数：
    detect_singing(text, extra_words=None, extra_regex="") -> (hit, title)
    prepare_lyrics(text, max_chars=30, max_segments=4) -> list[str]
"""

from __future__ import annotations

import re

# 明确「不是点歌」的说法（先判断；命中即不唱）
_NEGATIVE = [
    # 否定：不要唱 / 别唱了 / 甭来一首 / 我不听歌
    r"(不|别|甭|勿|没|不用|不必)[^，。,.!?！？]{0,3}(唱|来一首|点歌|听歌)",
    # 评价：唱得真好 / 唱歌好好听 / 唱的难听
    r"(唱得|唱的|唱起来)",
    r"唱(歌|歌儿)?[^，。,.!?！？]{0,3}(好听|难听|不错|真棒|跑调)",
    # 能力询问（不带具体歌名的）：你会唱歌吗 / 能不能唱
    r"(会|能|可以|擅长)(不会|不能|不可以)?唱(歌|歌儿)?[吗么嘛]?[?？]?$",
    # 陈述他人在唱：谁在唱歌 / 她们在唱
    r"(谁|有人|他们|她们|别人|隔壁|它)(在)?(唱歌|唱)",
    r"(在|正在)唱(歌)?[吗么]?[?？]?$",
    # 元话题：教我唱歌 / 聊聊唱歌 / 讨论唱歌
    r"教[^，。,.!?！？]{0,3}唱",
    r"(聊|说|讨论|讲|问)[^，。,.!?！？]{0,6}(歌|唱歌)",
    # 只是在放歌给我听：唱歌的吗 / 这是唱歌
    r"(这|那)(是|算)(在)?唱",
]

# 明确「要点歌」的说法（否定规则之后判断）
_POSITIVE = [
    r"唱歌模式",
    r"^(唱|来|点|听)[^，。,.!?！？]{0,8}$",              # 短句：唱歌 / 来一曲 / 点歌 / 唱两句
    r"(唱|来|点)(一|两|几|这|那)?(首|支|曲|段|个|句|遍)"
    r"[^，。,.!?！？]{0,18}",                            # 唱首歌 / 来一首小星星 / 给我唱一段
    r"(唱歌|歌曲|歌儿|儿歌|小曲)",
    r"(想|要|我要|我想)(听|你唱)[^，。,.!?！？]{0,10}(歌|首|曲)",
    r"点歌",
    r"《[^》]{1,20}》",
]

_SING_WORDS = {"唱歌", "唱首歌", "唱歌模式", "首歌", "歌曲", "歌儿", "小曲", "儿歌", "歌", "曲", "点歌"}
_TRAIL = re.compile(r"(这首歌|这首|那首|吧|呗|呀|啊|嘛|好吗|好不好|行吗|听听|给我听|听一下)[。！？~…\s]*$")
_LEAD = re.compile(r"^(请|麻烦|帮我|给我|再|快|你|阿凌|宝贝|来|唱|点|听)+")

_ACTION_RE = re.compile(r"[*＊][^*＊]{0,60}[*＊]")
_PAREN_RE = re.compile(r"[（(][^）)]{0,40}[）)]")
_MD_RE = re.compile(r"[*_`#>]+")
_EMOJI_RE = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff️‍]+"
)
_SPACE_RE = re.compile(r"[ \t　]+")


def detect_singing(text: str, extra_words: list[str] | None = None, extra_regex: str = "") -> tuple[bool, str]:
    """判断这句话是不是「点歌/唱歌」请求。返回 (是否唱歌, 歌名或空串)。"""
    if not text:
        return False, ""
    t = text.strip()
    if extra_words and any(w and w in t for w in extra_words):
        return True, _extract_title(t)
    for pat in _NEGATIVE:
        if re.search(pat, t):
            return False, ""
    for pat in _POSITIVE:
        if re.search(pat, t):
            return True, _extract_title(t)
    if extra_regex:
        try:
            if re.search(extra_regex, t):
                return True, _extract_title(t)
        except re.error:
            pass
    return False, ""


def _extract_title(text: str) -> str:
    """从点歌句里抠歌名；抠不出来返回空串（由 LLM 自己挑一首）。"""
    m = re.search(r"《([^》]{1,20})》", text)
    if m:
        return m.group(1).strip()

    # 「点歌 晴天」「点歌：晴天」
    m = re.search(r"点歌[\s：:，,]*([^\s，。,.!?！？]{1,16})", text)
    if m and m.group(1) not in _SING_WORDS:
        return m.group(1).strip()

    # 「来一首小星星」「唱两只老虎」「点首晴天」
    m = re.search(
        r"(?:唱|来|点|听)(?:一|两|几|这|那)?(?:首|支|曲|段|个|句|遍)?\s*"
        r"([一-龥A-Za-z0-9·\-]{1,16})",
        text,
    )
    if m:
        cand = _TRAIL.sub("", m.group(1)).strip()
        cand = _LEAD.sub("", cand)
        cand = re.sub(r"(模式|一下|一遍|一点|了|吧|呀|啊|呗)$", "", cand).strip()
        # 抠出来的只是「歌」「首歌」这类词，说明用户没点具体歌名
        if (cand and cand not in _SING_WORDS and len(cand) >= 2
                and not cand.startswith(("歌", "唱", "曲", "听"))):
            return cand
    return ""


def prepare_lyrics(text: str, max_chars: int = 30, max_segments: int = 4) -> list[str]:
    """把 LLM 给的歌词整理成 1~N 段可唱的文本。

    * 去掉 markdown、动作/括号描写、表情、多余空格；
    * 保留换行（换行对唱歌模型是换句提示），换成句号；
    * 单段超过 max_chars 字时按句末标点再切，最多 max_segments 段。
    """
    t = _ACTION_RE.sub("", text or "")
    t = _PAREN_RE.sub("", t)
    t = _EMOJI_RE.sub("", t)
    t = _MD_RE.sub("", t)
    t = t.replace("|", "，").replace("—", "，")
    t = _SPACE_RE.sub(" ", t)
    t = re.sub(r"《[^》]{0,20}》", "", t)   # 歌词里不念歌名书的书名号
    lines = [ln.strip(" ，,、") for ln in t.splitlines()]
    lines = [ln for ln in lines if ln]
    t = "。".join(lines) if lines else t
    t = re.sub(r"[。！？!?；;…~～]+\s*", lambda m: m.group(0)[0], t)
    t = re.sub(r"[，,、]{2,}", "，", t)
    t = t.strip(" ，。；：、")

    if not t:
        return []
    if max_chars > 0 and len(t) > max_chars:
        parts = [p for p in re.split(r"(?<=[。！？!?])", t) if p.strip()]
        out: list[str] = []
        buf = ""
        for p in parts:
            while len(p) > max_chars:
                if buf:
                    out.append(buf)
                    buf = ""
                out.append(p[:max_chars])
                p = p[max_chars:]
            if len(buf) + len(p) <= max_chars:
                buf += p
            else:
                if buf:
                    out.append(buf)
                buf = p
        if buf:
            out.append(buf)
        t_parts = out
    else:
        t_parts = [t]
    if max_segments > 0:
        t_parts = t_parts[:max_segments]
    return [p for p in t_parts if p.strip()]
