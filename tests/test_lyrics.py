"""真实歌词库（lyrics.py）的单元测试：歌名解析、LRC 解析、选段、查不到。

跑法：python3 -m pytest tests/test_lyrics.py -q
网络一律 mock，不跑真实请求。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "astrbot_plugin_aling_voice"))

import lyrics  # noqa: E402

# ---------------------------------------------------------------- 歌名 / 歌手
EXTRACT_CASES = [
    # (说的原话, 期望歌名, 期望歌手, 是否明确给了歌名)
    ("唱首孙燕姿的天黑黑", "天黑黑", "孙燕姿", True),
    ("来一首孙燕姿的歌", "", "孙燕姿", False),
    ("点歌 孙燕姿 遇见", "遇见", "孙燕姿", True),
    ("点歌：孤勇者", "孤勇者", "", True),
    ("来一首小星星", "小星星", "", True),
    ("唱首《两只老虎》", "两只老虎", "", True),
    ("唱首歌", "", "", False),
    ("我想听歌", "", "", False),
    ("不存在的歌名xyzabc", "", "", False),
]


def test_extract_song_request_cases():
    for text, title, singer, explicit in EXTRACT_CASES:
        got_title, got_singer, got_explicit = lyrics.extract_song_request(text)
        assert (got_title, got_singer, got_explicit) == (title, singer, explicit), text


def test_split_singer_from_title():
    assert lyrics.split_singer_from_title("孙燕姿的天黑黑") == ("天黑黑", "孙燕姿")
    assert lyrics.split_singer_from_title("我第一次听这首歌") == ("我第一次听这首歌", "")
    assert lyrics.split_singer_from_title("这个不存在的东西") == ("这个不存在的东西", "")
    assert lyrics.split_singer_from_title("孙燕姿的歌") == ("", "孙燕姿")


# ---------------------------------------------------------------- LRC 解析
SAMPLE_LRC = """[ti:天黑黑]
[ar:孙燕姿]
[00:00.00]天黑黑 - 孙燕姿
[00:01.00]作词：廖莹如
[00:12.00]我的小时候
[00:16.00]吵闹任性的时侯
[00:20.00]我的外婆总会唱歌哄我
[00:30.00]天黑黑 欲落雨
[00:34.00]天黑黑 黑黑
[00:60.00]离开小时候
[01:04.00]有了自己的生活
[01:08.00]新鲜的歌 新鲜的念头
[01:40.00]天黑黑 欲落雨
[01:44.00]天黑黑 黑黑
"""


def test_parse_lyric_drops_meta_and_credits():
    rows = lyrics.parse_lyric(SAMPLE_LRC)
    texts = [t for _, t in rows]
    # 抬头行只留下歌名（歌手名被剥掉），且后续会被 _fits 当作「整行等于歌名」丢掉
    assert not any("孙燕姿" in t for t in texts)
    assert all("作词" not in t for t in texts)  # 制作名单不念
    assert "我的小时候" in texts
    times = [t for t, _ in rows if t >= 12]
    assert times[:2] == [12.0, 16.0]


def test_build_segments_keeps_real_lines_and_is_bounded():
    segs = lyrics.build_segments(SAMPLE_LRC, "天黑黑", max_chars=40, max_segments=2)
    assert 1 <= len(segs) <= 2
    for s in segs:
        assert "天黑黑" in s or "小时候" in s or "外婆" in s
        assert len(s) <= 40 + 1          # 只多一个连接用的逗号
    # 「天黑黑 欲落雨」这种以歌名开头的真歌词不能被当成抬头删掉
    assert any("欲落雨" in s for s in segs)


def test_plain_lyrics_still_work():
    plain = "一闪一闪亮晶晶\n满天都是小星星\n挂在天上放光明\n好像许多小眼睛"
    segs = lyrics.build_segments(plain, "小星星", max_chars=40, max_segments=2)
    assert segs
    assert any("亮晶晶" in s for s in segs)


# ---------------------------------------------------------------- 排序 / 查不到
def _hit(title, singer, mid="m1", interval=200, source="qq"):
    return lyrics.SongHit(title=title, singer=singer, source=source, mid=mid, interval=interval)


def test_rank_hits_rejects_unrelated_titles():
    hits = [_hit("ピカチュウ", "某某"), _hit("不存在的歌名xyzabc", "")]
    got = lyrics.rank_hits(hits, "不存在的歌名xyzabc")
    assert [h.title for h in got] == ["不存在的歌名xyzabc"]


def test_rank_hits_prefers_right_singer():
    hits = [_hit("遇见", "翻唱歌手"), _hit("遇见", "孙燕姿")]
    got = lyrics.rank_hits(hits, "遇见", "孙燕姿")
    assert got[0].singer == "孙燕姿"


def test_find_song_not_found_without_network(monkeypatch):
    monkeypatch.setattr(lyrics, "search_qq", lambda *a, **k: [])
    monkeypatch.setattr(lyrics, "search_lrclib", lambda *a, **k: [])
    res = lyrics.find_song("不存在的歌名xyzabc")
    assert res.ok is False
    assert "没有可用原词" in res.reason


def test_find_song_happy_path_with_mocked_network(monkeypatch):
    monkeypatch.setattr(lyrics, "search_qq", lambda *a, **k: [_hit("天黑黑", "孙燕姿")])
    monkeypatch.setattr(lyrics, "fetch_qq_lyric", lambda *a, **k: SAMPLE_LRC)
    res = lyrics.find_song("天黑黑", "孙燕姿")
    assert res.ok and res.source == "qq"
    assert res.segments and all(s.strip() for s in res.segments)


def test_find_song_singer_only_does_not_pick_by_default(monkeypatch):
    monkeypatch.setattr(lyrics, "search_qq", lambda *a, **k: [_hit("我不难过", "孙燕姿")])
    monkeypatch.setattr(lyrics, "fetch_qq_lyric", lambda *a, **k: SAMPLE_LRC)
    res = lyrics.find_song("", "孙燕姿", allow_singer_pick=False)
    assert res.ok is False
    assert res.reason == "只给了歌手"
