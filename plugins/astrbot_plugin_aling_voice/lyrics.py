"""真实歌词库查询（纯函数 + 两个公开接口，方便单测）。

为什么要有这个模块
------------------
点歌以前是让对话模型「凭记忆写歌词」，实测结果是残词（《天黑黑》只吐出
「天黑黑 欲落雨。天黑黑 黑黑」，不是原词），查不到的歌还会被编一段出来。
现在改成去公开歌词库查**真实原词**，查不到就明说没查到。

数据源（按顺序试，任一成功即返回）
--------------------------------
1. QQ 音乐：搜索 `c.y.qq.com/soso/fcgi-bin/client_search_cp`，歌词
   `c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg`，歌词带逐行时间轴。
2. lrclib.net：`/api/search`，返回 syncedLyrics（LRC 文本，带时间轴）。

选段
----
不是整首灌进去，而是按「开头 + 最重复的一段（通常是副歌）」各取 1~2 句，
拼成 2 段、每段不超过 max_chars 字，合计大致 40~80 秒的唱段。

对外接口
--------
    extract_song_request(text) -> (title, singer, explicit)
    search_qq(title, singer) -> list[SongHit]
    fetch_qq_lyric(mid) / fetch_lrclib_lyric(rec_id) -> raw LRC / 纯文本
    build_segments(raw, title, max_chars, max_segments) -> list[str]
    find_song(title, singer) -> LyricsResult
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://y.qq.com/",
}
_QQ_SEARCH = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
_QQ_LYRIC = "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg"
_LRCLIB = "https://lrclib.net/api/search"

# LRC 行：[mm:ss.xx]或[mm:ss:xx]
_TS_RE = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_META_RE = re.compile(r"^\[(ti|ar|al|by|offset|kana|re|ve|length):.*\]$", re.I)
# 制作名单行（不是歌词）
_CREDIT_RE = re.compile(
    r"^(作词|作曲|编曲|词|曲|混音|母带|监制|制作人|和声|吉他|贝斯|鼓|录音|出品|发行|"
    r"OP|SP|原唱|翻唱|演唱|出品人|统筹|企划|营销|策划)\s*[：:/]"
)
_BRACKET_RE = re.compile(r"[【\[][^】\]]{0,30}[】\]]")
_TITLE_TAIL_RE = re.compile(r"\s*[-–—]\s*[^-–—]{0,30}$")  # "天黑黑 - 孙燕姿"


@dataclass
class SongHit:
    title: str
    singer: str
    source: str
    mid: str = ""          # QQ 音乐 songmid
    interval: int = 0      # 秒
    lrclib_id: int = 0
    synced: bool = False


@dataclass
class LyricsResult:
    ok: bool
    title: str = ""
    singer: str = ""
    source: str = ""
    segments: list[str] = field(default_factory=list)
    reason: str = ""       # 失败原因（给日志，不直接念给用户）
    candidates: list[str] = field(default_factory=list)
    picked_by_singer: bool = False


# ---------------------------------------------------------------- 歌名 / 歌手拆解
# 这些词出现在「X的Y」左边时不是歌手名
_NOT_SINGER = {
    "我", "你", "他", "她", "它", "我们", "你们", "他们", "她们", "咱们", "大家",
    "这", "那", "这个", "那个", "一个", "一首", "刚才", "刚刚", "上面", "下面",
    "喜欢", "最爱", "经典", "好听", "快乐", "悲伤", "童年", "小时候", "妈妈", "爸爸",
    "外婆", "爷爷", "奶奶", "宝宝", "宝贝", "孩子", "儿童", "快乐", "浪漫", "温柔",
}
# 出现在「的」左边但不是歌手名的开头
_NOT_SINGER_PREFIX = (
    "不存在", "没有", "这首", "那首", "刚才", "刚刚", "上面", "下面", "什么", "哪",
    "一个", "一首", "这个", "那个", "别人", "他们", "她们",
)
# 歌手名常见结尾（用于「孙燕姿 遇见」这种无「的」写法）
_SINGER_SUFFIX = ("乐队", "组合", "合唱团", "乐团", "儿歌", "多多", "巴士", "虎")


def _clean_piece(s: str) -> str:
    s = (s or "").strip(" 　，,。.!！?？~～、:：\"'“”‘’")
    return s


def split_singer_from_title(cand: str) -> tuple[str, str]:
    """把「孙燕姿的天黑黑」拆成（歌名=天黑黑, 歌手=孙燕姿）。

    只在左边像人名、右边还像歌名时才拆；拆不出来返回（cand, ""）。
    """
    cand = _clean_piece(cand)
    if "的" not in cand:
        return cand, ""
    left, _, right = cand.rpartition("的")
    left, right = _clean_piece(left), _clean_piece(right)
    if not left or not right:
        return cand, ""
    if left in _NOT_SINGER or right in _NOT_SINGER:
        return cand, ""
    if left.startswith(_NOT_SINGER_PREFIX):
        return cand, ""
    if right in _GENERIC:
        return "", left          # 「孙燕姿的歌」→ 只给了歌手
    if len(left) < 2 or len(left) > 12 or len(right) < 2:
        return cand, ""
    # 右边本身又带「的」说明拆错了（如「你的我的歌」）
    if "的" in right:
        return cand, ""
    return right, left


def extract_song_request(text: str) -> tuple[str, str, bool]:
    """从点歌句里取（歌名, 歌手, 是否用户明确给了歌名）。

    规则与 sing._extract_title 保持一致（同样从《》、「点歌 X」、「来一首 X」
    里取），额外做歌手名剥离。抠不出歌名时歌名返回空串。
    """
    t = (text or "").strip()
    if not t:
        return "", "", False

    # 《X》最可靠：括号里整串就是歌名，不再拆歌手
    m = re.search(r"《([^》]{1,24})》", t)
    if m:
        title = _clean_piece(m.group(1))
        return title, "", bool(title)

    # 「点歌 孙燕姿 遇见」「点歌：晴天」
    m = re.search(r"点歌[\s：:，,]*([^\s，。,.!?！？]{1,16}(?:[\s　]+[^\s，。,.!?！？]{1,16})?)", t)
    if m:
        rest = _clean_piece(m.group(1))
        parts = [_clean_piece(x) for x in re.split(r"[\s　]+", rest) if _clean_piece(x)]
        if len(parts) >= 2:
            return parts[1], parts[0], True
        title, singer = split_singer_from_title(rest)
        if title and title not in _GENERIC:
            return title, singer, True

    # 「来一首小星星」「唱首孙燕姿的天黑黑」「来一首孙燕姿的歌」
    m = re.search(
        r"(?:唱|来|点|听)(?:一|两|几|这|那)?(?:首|支|曲|段|个|句|遍)?\s*"
        r"([一-龥A-Za-z0-9·\-]{1,24})",
        t,
    )
    if not m:
        return "", "", False
    cand = _TRAIL_RE.sub("", _clean_piece(m.group(1)))
    cand = _LEAD_RE.sub("", cand)
    cand = re.sub(r"(模式|一下|一遍|一点|了|吧|呀|啊|呗)$", "", cand).strip()
    if not cand or cand in _GENERIC or len(cand) < 2:
        return "", "", False
    if cand.startswith(("歌", "唱", "曲", "听")):
        return "", "", False

    title, singer = split_singer_from_title(cand)
    if not title:
        return "", singer, False      # 「来一首孙燕姿的歌」：只给了歌手
    if title in _GENERIC or title in {"歌", "歌曲", "儿歌"}:
        return "", cand, False
    return title, singer, True


_GENERIC = {"歌", "歌曲", "歌儿", "首歌", "小曲", "儿歌", "唱歌", "点歌", "一曲", "一段"}
_TRAIL_RE = re.compile(r"(这首歌|这首|那首|吧|呗|呀|啊|嘛|好吗|好不好|行吗|听听|给我听|听一下)[。！？~…\s]*$")
_LEAD_RE = re.compile(r"^(请|麻烦|帮我|给我|再|快|你|阿凌|宝贝|来|唱|点|听)+")


# ---------------------------------------------------------------- 网络（同步，调用方放线程里跑）
def _get(url: str, timeout: float = 6.0) -> bytes:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def search_qq(title: str, singer: str = "", limit: int = 10, timeout: float = 6.0,
              rank: bool = True) -> list[SongHit]:
    """QQ 音乐搜歌；给了歌手就优先收该歌手的版本（先精确再模糊）。"""
    kw = f"{title} {singer}".strip()
    url = (f"{_QQ_SEARCH}?w={urllib.parse.quote(kw)}&format=json&n={limit}"
           "&p=1&cr=1&new_json=1&t=0")
    try:
        obj = json.loads(_get(url, timeout) or b"{}")
    except Exception:  # noqa: BLE001
        return []
    songs = (((obj.get("data") or {}).get("song") or {}).get("list") or [])
    hits: list[SongHit] = []
    for s in songs:
        name = _clean_piece(s.get("name") or "")
        sing = ",".join(_clean_piece(x.get("name") or "") for x in (s.get("singer") or []))
        mid = s.get("mid") or ""
        if not name or not mid:
            continue
        hits.append(SongHit(title=name, singer=sing, source="qq", mid=mid,
                            interval=int(s.get("interval") or 0)))
    return rank_hits(hits, title, singer) if rank else hits


def rank_hits(hits: list[SongHit], title: str, singer: str = "") -> list[SongHit]:
    """排序：同名 → 同歌手 → 非 Live/伴奏 → 时长不太离谱。"""
    tn = _norm(title)
    sn = _norm(singer)
    scored = []
    for h in hits:
        score = 0
        ht = _norm(h.title)
        if ht == tn:
            score += 100
        elif tn and tn in ht:
            score += 40
        elif ht and ht in tn and len(ht) >= max(2, len(tn) - 6):
            score += 30
        else:
            continue          # 标题对不上，直接不要
        if sn:
            if sn in _norm(h.singer) or _norm(h.singer) in sn:
                score += 60
            else:
                score -= 40
        if re.search(r"(live|演唱会|伴奏|纯音乐|remix|dj|版)", h.title, re.I):
            score -= 30
        if 60 <= h.interval <= 420:
            score += 5
        if h.synced or h.mid:
            score += 2
        scored.append((score, h))
    scored.sort(key=lambda x: -x[0])
    return [h for s, h in scored if s > 0]


def search_lrclib(title: str, singer: str = "", timeout: float = 6.0) -> list[SongHit]:
    url = f"{_LRCLIB}?q={urllib.parse.quote((title + ' ' + singer).strip())}"
    try:
        arr = json.loads(_get(url, timeout) or b"[]")
    except Exception:  # noqa: BLE001
        return []
    hits = []
    for it in arr or []:
        name = _clean_piece(it.get("trackName") or "")
        sing = _clean_piece(it.get("artistName") or "")
        if not name:
            continue
        hits.append(SongHit(title=name, singer=sing, source="lrclib",
                            interval=int(it.get("duration") or 0),
                            lrclib_id=int(it.get("id") or 0),
                            synced=bool(it.get("syncedLyrics"))))
    return rank_hits(hits, title, singer)


def fetch_qq_lyric(mid: str, timeout: float = 6.0) -> str:
    url = f"{_QQ_LYRIC}?songmid={mid}&format=json&nobase64=1&g_tk=5381"
    try:
        obj = json.loads(_get(url, timeout) or b"{}")
    except Exception:  # noqa: BLE001
        return ""
    return str(obj.get("lyric") or "")


def fetch_lrclib_lyric(rec_id: int, timeout: float = 6.0) -> str:
    url = f"https://lrclib.net/api/get/{rec_id}"
    try:
        obj = json.loads(_get(url, timeout) or b"{}")
    except Exception:  # noqa: BLE001
        return ""
    return str(obj.get("syncedLyrics") or obj.get("plainLyrics") or "")


# ---------------------------------------------------------------- 歌词解析 / 选段
def parse_lyric(raw: str) -> list[tuple[float, str]]:
    """解析 LRC / 纯文本歌词，返回（秒, 文本）；纯文本给 -1 作为时间。"""
    out: list[tuple[float, str]] = []
    for line in (raw or "").splitlines():
        s = line.strip()
        if not s or _META_RE.match(s):
            continue
        stamps = []
        rest = s
        while True:
            m = _TS_RE.match(rest)
            if not m:
                break
            mm, ss, frac = m.group(1), m.group(2), m.group(3) or "0"
            sec = int(mm) * 60 + int(ss) + float("0." + frac.ljust(3, "0"))
            stamps.append(sec)
            rest = rest[m.end():]
        text = _clean_lyric_line(rest)
        if not text:
            continue
        if stamps:
            for sec in stamps:
                out.append((sec, text))
        else:
            out.append((-1.0, text))
    return out


def _clean_lyric_line(s: str) -> str:
    s = _BRACKET_RE.sub("", s)
    s = s.strip(" 　\t")
    s = s.rstrip("，,、；;：:。.！!？?~～…")
    # 只处理「歌名 - 歌手」这种抬头行（歌词正文里很少出现两端带空格的破折号）
    if re.search(r"\s[-–—]\s", s) and len(s) <= 40:
        s = re.split(r"\s[-–—]\s", s)[0].strip()
    if not s or _CREDIT_RE.match(s):
        return ""
    if re.fullmatch(r"[\W_]+", s):
        return ""
    if len(s) < 2:
        return ""
    return s


def _norm(s: str) -> str:
    return re.sub(r"[\s　，,。.!！?？~～、:：\"'“”‘’（）()《》\-]+", "", s or "").lower()


def _fits(line: str, title: str) -> bool:
    """这行像不像歌词：不是歌名复读、不是制作信息。

    注意只排除「整行等于歌名」的行（歌名抬头/制作名单），像
    「天黑黑 欲落雨」这种以歌名开头但确实是歌词的不能误删。
    """
    n = _norm(line)
    if not n:
        return False
    if n == _norm(title):
        return False
    if re.search(r"^(作词|作曲|编曲|词|曲|原唱|翻唱)\s*[:：]", line):
        return False
    return True


def _join(lines: list[str], max_chars: int) -> str:
    out = ""
    for ln in lines:
        if not out:
            out = ln
        elif len(out) + 1 + len(ln) <= max_chars:
            out += "，" + ln
        else:
            break
    return out


def build_segments(raw: str, title: str, max_chars: int = 40,
                   max_segments: int = 2) -> list[str]:
    """从原词里选「开头 + 最重复的一段（副歌）」，拼成可唱的 N 段。"""
    parsed = parse_lyric(raw)
    if not parsed:
        return []
    dur = max((t for t, _ in parsed if t >= 0), default=0.0)
    body = [(t, _clean_lyric_line(txt)) for t, txt in parsed]
    body = [(t, x) for t, x in body if x and _fits(x, title)]
    # 去掉相邻重复行（很多歌词会把副歌贴两遍）
    dedup: list[tuple[float, str]] = []
    for t, x in body:
        if dedup and _norm(dedup[-1][1]) == _norm(x):
            continue
        dedup.append((t, x))
    body = dedup
    if not body:
        return []

    timed = all(t >= 0 for t, _ in body) and dur > 30
    if timed:
        head = [x for t, x in body if 0.03 * dur <= t <= 0.35 * dur][:3]
        chorus = _most_repeated_block(body, dur)
    else:
        head = [x for _, x in body][:2]
        chorus = _most_repeated_block(body, 0.0)

    picks: list[str] = []
    used: set[str] = set()

    def add(seg: str) -> None:
        n = _norm(seg)
        if seg and n and n not in used:
            used.add(n)
            picks.append(seg)

    for block in (head, chorus):
        add(_join([x for x in block if x], max_chars))

    # 不够段数：按顺序把还没用过的句子打包补上（每段尽量凑满 max_chars）
    i = 0
    while len(picks) < max_segments and i < len(body):
        pack: list[str] = []
        while i < len(body) and (not pack or len(_join(pack + [body[i][1]], max_chars)) <= max_chars):
            line_n = _norm(body[i][1])
            if line_n in used or any(line_n in _norm(p) for p in picks):
                i += 1
                continue
            pack.append(body[i][1])
            i += 1
            if len(_join(pack, max_chars)) >= max_chars - 6:
                break
        if pack:
            add(_join(pack, max_chars))
        elif i >= len(body):
            break
    return picks[:max_segments]


def _most_repeated_block(body: list[tuple[float, str]], dur: float,
                         block: int = 2) -> list[str]:
    """找重复次数最多的连续 block 句（= 副歌），返回这几句。"""
    texts = [x for _, x in body]
    counter: dict[tuple[str, ...], list[int]] = {}
    for i in range(0, len(texts) - block + 1):
        key = tuple(_norm(x) for x in texts[i:i + block])
        counter.setdefault(key, []).append(i)
    best_key, best_pos = None, []
    for key, pos in counter.items():
        if len(pos) < 2:
            continue
        if best_key is None or len(pos) > len(best_pos):
            best_key, best_pos = key, pos
    if best_key:
        i = best_pos[0]
        return texts[i:i + block]
    # 没有重复段：取接近 55% 位置的两句
    if not texts:
        return []
    if dur > 30:
        target = 0.55 * dur
        idx = min(range(len(body)), key=lambda k: abs(body[k][0] - target))
    else:
        idx = len(texts) // 2
    return texts[idx:idx + block]


# ---------------------------------------------------------------- 对外主入口
def find_song(title: str, singer: str = "", max_chars: int = 40,
              max_segments: int = 2, timeout: float = 6.0,
              allow_singer_pick: bool = True) -> LyricsResult:
    """查一首歌的真实原词并选好唱段；查不到返回 ok=False 且写明原因。

    只给了歌手（没给歌名）时，若 allow_singer_pick，会取该歌手最匹配的一首，
    并在 picked_by_singer 里标记，调用方应把歌名告诉用户。
    """
    title, singer = _clean_piece(title), _clean_piece(singer)
    picked = False
    if not title and singer:
        if not allow_singer_pick:
            return LyricsResult(False, reason="只给了歌手")
        # 用歌手名搜，取第一条该歌手的歌
        for hit in search_qq(singer, "", limit=10, timeout=timeout, rank=False):
            if _norm(singer) and _norm(singer) in _norm(hit.singer):
                title, picked = hit.title, True
                break
        if not title:
            return LyricsResult(False, singer=singer, reason="没查到这个歌手的歌")
    if not title:
        return LyricsResult(False, reason="没有歌名")

    tried: list[str] = []
    for hit in search_qq(title, singer, limit=10, timeout=timeout):
        tried.append(f"qq:{hit.title}/{hit.singer}")
        raw = fetch_qq_lyric(hit.mid, timeout)
        segs = build_segments(raw, hit.title, max_chars, max_segments)
        if segs:
            return LyricsResult(True, title=hit.title, singer=hit.singer, source="qq",
                                segments=segs, candidates=tried, picked_by_singer=picked)

    for hit in search_lrclib(title, singer, timeout=timeout):
        tried.append(f"lrclib:{hit.title}/{hit.singer}")
        raw = fetch_lrclib_lyric(hit.lrclib_id, timeout)
        segs = build_segments(raw, hit.title, max_chars, max_segments)
        if segs:
            return LyricsResult(True, title=hit.title, singer=hit.singer, source="lrclib",
                                segments=segs, candidates=tried, picked_by_singer=picked)

    return LyricsResult(False, title=title, singer=singer, reason="两个歌词库都没有可用原词",
                        candidates=tried, picked_by_singer=picked)
