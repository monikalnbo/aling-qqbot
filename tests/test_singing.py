"""唱歌模式的意图识别 / 歌词整理单元测试。

跑法：python3 -m pytest tests/test_singing.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "astrbot_plugin_aling_voice"))

import sing  # noqa: E402

# 正例：用户表达「想听歌/点歌」，必须触发
POSITIVE = [
    ("唱歌", ""),
    ("唱首歌", ""),
    ("唱首《两只老虎》", "两只老虎"),
    ("来一首小星星", "小星星"),
    ("我要听歌", ""),
    ("点歌 晴天", "晴天"),
    ("点歌：孤勇者", "孤勇者"),
    ("唱歌模式", ""),
    ("给我唱一段", ""),
    ("能唱《生日快乐》吗", "生日快乐"),
    ("唱个歌呗", ""),
    ("来一曲", ""),
    ("想听歌了", ""),
    ("唱两句吧", ""),
    ("给我唱首歌吧", ""),
]

# 反例：正常聊天，绝不能误触发
NEGATIVE = [
    "不要唱歌",
    "别唱了",
    "你会唱歌吗",
    "他唱得真好听",
    "谁在唱歌",
    "聊聊天吧",
    "今天天气不错",
    "聊聊别的",
    "我不想听歌",
    "教我唱歌",
    "在唱歌吗",
    "她唱歌好好听",
    "我不听歌",
    "你们在唱歌吗",
    "这首歌是谁唱的",
    "明天早上八点叫我起床",
]


def test_positive_detects():
    bad = []
    for text, want_title in POSITIVE:
        hit, title = sing.detect_singing(text)
        if not hit or title != want_title:
            bad.append((text, hit, title, want_title))
    assert not bad, f"正例未按预期触发：{bad}"


def test_negative_does_not_detect():
    bad = []
    for text in NEGATIVE:
        hit, title = sing.detect_singing(text)
        if hit:
            bad.append((text, title))
    assert not bad, f"反例被误判为点歌：{bad}"


def test_extra_words_and_regex():
    assert sing.detect_singing("整一个来听听", ["整一个"])[0] is True
    assert sing.detect_singing("给我嚎一段弯弯的月亮", extra_regex=r"嚎一段")[0] is True
    assert sing.detect_singing("今天中午吃啥", ["整一个"])[0] is False


def test_prepare_lyrics_cleans():
    raw = "《小星星》\n*眨眨眼*\n（轻声）一闪一闪亮晶晶，满天都是小星星。\n挂在天上放光明，好像许多小眼睛。"
    out = sing.prepare_lyrics(raw, max_chars=30, max_segments=4)
    assert out, "歌词不该为空"
    joined = "".join(out)
    assert "《" not in joined and "*" not in joined and "（" not in joined
    assert "一闪一闪亮晶晶" in joined
    assert all(len(x) <= 30 for x in out)


def test_prepare_lyrics_segments_limited():
    long_text = "。".join([f"第{i}句歌词内容" for i in range(1, 40)]) + "。"
    out = sing.prepare_lyrics(long_text, max_chars=20, max_segments=4)
    assert len(out) <= 4
    assert all(len(x) <= 20 for x in out)


def test_prepare_lyrics_empty():
    assert sing.prepare_lyrics("**\n（）\n***", 30, 4) == []
